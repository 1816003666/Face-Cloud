"""设备编排抽象层。

- simulator：纯内存 Mock，免 Docker，任意平台可直接跑（开发/演示默认）。
- redroid：真实 Docker Android 容器 + ADB 操控，支持截图、点按、滑动、文本、按键等。

所有设备均绑定到 CLOUD_PHONE_SERVER 指定的云手机服务器，serial 采用 ADB 风格
`server_ip:port`。

redroid 容器通过 SSH 远程管理：创建时在服务器上 docker run，删除时 docker rm -f。
"""
import json
import os
import random
import re
import subprocess
import threading
import time
import concurrent.futures
from datetime import datetime, timezone

from flask import current_app

def _read_frame_size():
    """推流帧尺寸 (宽, 高)；环境变量里设为 0 或留空 = 用设备原生分辨率。"""
    w = os.environ.get("STREAM_FRAME_WIDTH", "720").strip()
    h = os.environ.get("STREAM_FRAME_HEIGHT", "1280").strip()
    try:
        w_i, h_i = int(w), int(h)
        if w_i > 0 and h_i > 0:
            return (w_i, h_i)
    except ValueError:
        pass
    return None


# 推流分辨率。内网设备原生 1080x1920，多路同时看时浏览器软解码（Broadway）
# 扛不住，统一缩到 720p；帧率由设备决定（screenrecord 没有 --fps 参数，
# 这批设备实测就是 60Hz）。
STREAM_FRAME_SIZE = _read_frame_size()

# 内存态：serial -> {status, ip, fingerprint}（simulator 用）
_SIM_STATE: dict[str, dict] = {}
# 已分配的 ADB 端口集合（simulator 用）
_ALLOCATED_PORTS: set[int] = set()


def _now():
    return datetime.now(timezone.utc)


def _server_ip() -> str:
    try:
        return current_app.config["CLOUD_PHONE_SERVER"]
    except RuntimeError:
        import os
        return os.environ.get("CLOUD_PHONE_SERVER", "127.0.0.1")


def _adb_start_port() -> int:
    try:
        return current_app.config["CLOUD_PHONE_ADB_START_PORT"]
    except RuntimeError:
        import os
        return int(os.environ.get("CLOUD_PHONE_ADB_START_PORT", "5555"))


def _adb_path() -> str:
    """获取 adb 可执行文件路径。"""
    try:
        p = current_app.config.get("ADB_PATH", "")
    except RuntimeError:
        p = os.environ.get("ADB_PATH", "")
    if p and os.path.isfile(p):
        return p
    # 常见路径回退
    candidates = [
        r"C:\Users\熏香花朵凛然绽放\Desktop\1200台云手机部署\platform-tools\adb.exe",
        "adb",
    ]
    for c in candidates:
        if c == "adb":
            return "adb"  # 依赖 PATH
        if os.path.isfile(c):
            return c
    return "adb"


def _run_adb(args: list, timeout: int = 15) -> tuple:
    """执行 adb 命令，返回 (returncode, stdout, stderr)。"""
    cmd = [_adb_path()] + args
    try:
        # Windows: CREATE_NO_WINDOW 避免弹出控制台
        creationflags = 0x08000000 if os.name == "nt" else 0
        p = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            creationflags=creationflags,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, b"", b"timeout"
    except FileNotFoundError:
        return -1, b"", b"adb not found"


# ---------------------------------------------------------------------------
# ADB 连接预热 / 确保连接
# ---------------------------------------------------------------------------
# 已成功 connect 的 adb 设备集合（adb 连接 persistent，一旦建立持续保持）。
# 避免前端 240 台首屏并发首次 connect 时，各服务器 sshd 隧道瞬时转发限流导致截图失败。
_ADB_CONNECTED: set[str] = set()
_ADB_CONNECTED_LOCK = threading.Lock()


# `adb devices` 结果缓存（秒）。上百台设备时这条命令本身也要跑几秒，
# 高频调用会自成一堵墙，所以短时间内复用一次结果。
_ADB_DEVICES_CACHE = {"ts": 0.0, "serials": set()}
_ADB_DEVICES_CACHE_TTL = 3.0
_ADB_DEVICES_LOCK = threading.Lock()


def _online_adb_serials(force: bool = False) -> set:
    now = time.time()
    with _ADB_DEVICES_LOCK:
        if not force and now - _ADB_DEVICES_CACHE["ts"] < _ADB_DEVICES_CACHE_TTL:
            return _ADB_DEVICES_CACHE["serials"]
    serials = set()
    try:
        rc, out, _ = _run_adb(["devices"], timeout=10)
        if rc == 0 and out:
            for line in out.decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.endswith("device") and "\t" in line:
                    serials.add(line.split("\t")[0].strip())
    except Exception:  # noqa: BLE001
        return _ADB_DEVICES_CACHE["serials"]
    with _ADB_DEVICES_LOCK:
        _ADB_DEVICES_CACHE["ts"] = now
        _ADB_DEVICES_CACHE["serials"] = serials
    return serials


def _adb_device_online(serial: str) -> bool:
    """设备在 `adb devices` 中且状态为 device（不含 offline/unauthorized）。"""
    return serial in _online_adb_serials()


def _ensure_adb_connected(serial: str) -> bool:
    """确保 adb 已连接到该设备（serial 形如 server_ip:port）。连接成功后保持。

    两处性能关键（实测公网设备原实现单次调用要 **41 秒**）：
    1. 先查一次 `adb devices`：设备若已在线就直接返回，不再走 adb connect。
       盲目 connect 对**已在线**的公网设备同样会耗满超时——这是截图请求堆积的主因。
    2. connect 超时 20s → 10s、重试 2 次 → 1 次：不可达的 IP 快速失败，
       避免 200+ 台离线设备把巡检/截图线程全部占死。
    """
    if serial in _ADB_CONNECTED:
        return True
    # 设备已经在线就不必再 connect（公网设备 connect 一次可能耗满超时）
    if _adb_device_online(serial):
        with _ADB_CONNECTED_LOCK:
            _ADB_CONNECTED.add(serial)
        return True
    rc, out, err = _run_adb(["connect", serial], timeout=10)
    merged = ((out or b"") + (err or b"")).lower()
    if rc == 0 and (b"connected" in merged or b"already" in merged):
        with _ADB_CONNECTED_LOCK:
            _ADB_CONNECTED.add(serial)
        return True
    return False


def _forget_adb_connection(serial: str):
    with _ADB_CONNECTED_LOCK:
        _ADB_CONNECTED.discard(serial)


def warmup_adb_connections(serials: list, batch: int = 12, gap: float = 0.2) -> tuple:
    """批量预热 adb 连接（分批错峰）。返回 (成功数, 失败列表)。"""
    ok, failed = 0, []

    def _one(s: str) -> tuple:
        return _ensure_adb_connected(s), s

    for i in range(0, len(serials), batch):
        chunk = serials[i:i + batch]
        with concurrent.futures.ThreadPoolExecutor(max_workers=batch) as ex:
            for okk, s in ex.map(_one, chunk):
                if okk:
                    ok += 1
                else:
                    failed.append(s)
        time.sleep(gap)
    return ok, failed


# ---------------------------------------------------------------------------
# Simulator 后端（保留）
# ---------------------------------------------------------------------------
def _alloc_port() -> int:
    port = _adb_start_port()
    while port in _ALLOCATED_PORTS:
        port += 2
    _ALLOCATED_PORTS.add(port)
    return port


def _free_port(port: int):
    _ALLOCATED_PORTS.discard(port)


def _fake_fp():
    return json.dumps({
        "imei": f"{random.randint(10**14, 10**15-1)}",
        "android_id": f"{random.randint(10**15, 10**16-1):x}",
        "model": "Pixel-Cloud",
        "sku": "iOS-skin",
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Redroid / 真实 ADB 后端
# ---------------------------------------------------------------------------
def list_remote_adb_devices():
    """在远端服务器上执行 adb devices，返回 [{serial, status}]。"""
    cli = _ssh_shared_client()
    rc, out, _err = _ssh_exec(cli, "adb devices", timeout=10)
    rows = []
    for line in (out or "").splitlines()[1:]:
        line = line.strip()
        if not line or "\t" not in line:
            continue
        serial, _, status = line.partition("\t")
        rows.append({"serial": serial.strip(), "status": status.strip()})
    return rows


def _adb_serial_state(serial: str) -> str:
    """查询单个 serial 的 adb 状态（本机 adb）。"""
    try:
        rc, out, _err = _run_adb(["-s", serial, "get-state"], timeout=6)
        return (out or b"").decode("utf-8", "ignore").strip() if rc == 0 else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def list_adb_devices() -> list[dict]:
    """列出当前已连接的 ADB 设备（含离线）。"""
    rc, out, err = _run_adb(["devices", "-l"], timeout=10)
    devices = []
    if rc != 0:
        return devices
    text = out.decode("utf-8", errors="replace") if isinstance(out, bytes) else out
    for line in text.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial = parts[0]
        status = parts[1]
        info = {"serial": serial, "status": status}
        # 解析额外信息 model / device / transport_id
        for p in parts[2:]:
            if ":" in p:
                k, v = p.split(":", 1)
                info[k] = v
        devices.append(info)
    return devices


def adb_connect(serial: str) -> bool:
    """连接一个远程 ADB 设备。"""
    rc, out, err = _run_adb(["connect", serial], timeout=10)
    text = (out + err).decode("utf-8", errors="replace").lower()
    return "connected" in text or "already" in text


def _adb_ascii_dir() -> str:
    """纯 ASCII 临时目录（adb.exe 对含中文/非 ASCII 的本地路径支持不佳）。"""
    d = os.path.join(os.environ.get("SystemDrive", "C:") + os.sep, "adb_tmp")
    os.makedirs(d, exist_ok=True)
    return d


def _adb_ascii_copy(local_path: str) -> str:
    """把本地文件复制到纯 ASCII 临时路径（唯一文件名，避免并发冲突），返回复制后的路径（用完需清理）。"""
    import shutil
    import threading
    stem, ext = os.path.splitext(os.path.basename(local_path))
    tmp = os.path.join(_adb_ascii_dir(), f"{stem}.{os.getpid()}.{threading.get_ident()}{ext}")
    shutil.copy2(local_path, tmp)
    return tmp


def push_file_to_device(serial: str, local_path: str, remote_path: str, timeout: int = 180) -> dict:
    """把本地文件真实推送到设备（adb push），返回结果与输出。"""
    import shutil
    try:
        adb_connect(serial)  # 确保已连接（连接会断，需自动重连）
    except Exception:  # noqa: BLE001
        pass
    src = local_path
    try:
        if not _is_ascii_path(local_path):
            src = _adb_ascii_copy(local_path)
        rc, out, err = _run_adb(["-s", serial, "push", src, remote_path], timeout=timeout)
    finally:
        if src != local_path:
            try:
                os.remove(src)
            except OSError:
                pass
    text = (out + err).decode("utf-8", errors="replace").strip()
    return {"ok": rc == 0, "output": text, "remote_path": remote_path}


def list_device_dir(serial: str, path: str = "/sdcard") -> dict:
    """列出云手机上指定目录的文件（adb shell ls -la 解析），返回目录/文件条目。"""
    path = path or "/sdcard"
    if path == "/sdcard":
        path = "/sdcard/"
    rc, out, err = _adb_shell(serial, f"ls -la {path}", timeout=20)
    if rc != 0:
        return {"ok": False, "error": err.decode(errors="ignore").strip() or f"无法访问 {path}"}
    items = []
    lines = out.decode(errors="replace").splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("total"):
            continue
        parts = line.split()
        if len(parts) < 7:
            continue
        perms = parts[0]
        if len(perms) < 10 or perms[0] not in "dl-":
            continue
        # Android toybox ls：权限 链接数 属主 属组 大小 YYYY-MM-DD HH:MM 名称（日期2段）
        name_start = 7
        if not (len(parts[5]) == 10 and parts[5][4] == "-"):
            name_start = 8  # 老式日期 Jan 1 2023（3段）
        name = " ".join(parts[name_start:])
        if name in (".", ".."):
            continue
        link_target = None
        if " -> " in name:
            name, link_target = name.split(" -> ", 1)
            name = name.strip()
        mtime = " ".join(parts[5:name_start])
        items.append({
            "name": name,
            "path": f"{path.rstrip('/')}/{name}",
            "is_dir": perms.startswith("d"),
            "is_link": perms.startswith("l"),
            "size": int(parts[4]) if parts[4].isdigit() else 0,
            "perms": perms,
            "mtime": mtime,
        })
    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {"ok": True, "path": path, "items": items}


def read_device_file(serial: str, path: str, max_bytes: int = 150000) -> dict:
    """读取云手机上文本文件内容（head -c 截断，避免大文件刷屏）。"""
    if not path:
        return {"ok": False, "error": "缺少路径"}
    rc, out, err = _adb_shell(serial, f"head -c {max_bytes} {path}", timeout=20)
    if rc != 0:
        return {"ok": False, "error": err.decode(errors="ignore").strip() or f"无法读取 {path}"}
    data = out
    return {
        "ok": True,
        "content": data.decode(errors="replace")[:max_bytes],
        "bytes": len(data),
        "truncated": len(data) >= max_bytes,
    }


def pull_file_from_device(serial: str, remote_path: str, local_path: str, timeout: int = 120) -> dict:
    """从设备拉取文件到本地（adb pull）。"""
    import shutil
    try:
        adb_connect(serial)
    except Exception:  # noqa: BLE001
        pass
    dst = local_path
    try:
        if not _is_ascii_path(local_path):
            dst = os.path.join(_adb_ascii_dir(), os.path.basename(local_path) or "pulled.bin")
        rc, out, err = _run_adb(["-s", serial, "pull", remote_path, dst], timeout=timeout)
        if rc == 0 and dst != local_path and os.path.isfile(dst):
            shutil.copy2(dst, local_path)
    finally:
        if dst != local_path:
            try:
                os.remove(dst)
            except OSError:
                pass
    text = (out + err).decode("utf-8", errors="replace").strip()
    return {"ok": rc == 0 and os.path.isfile(local_path), "output": text, "local_path": local_path}


def _is_ascii_path(path: str) -> bool:
    try:
        path.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def install_apk_to_device(serial: str, local_path: str, timeout: int = 300) -> dict:
    """把本地 APK 安装到设备（adb install -r 流式安装，路径参数为本机路径）。

    adb install 的路径参数是本地文件路径，会自动流式传输到设备安装，
    无需（也不能）先 push 到 /data/local/tmp。
    """
    import shutil
    src = local_path
    try:
        adb_connect(serial)
    except Exception:  # noqa: BLE001
        pass
    try:
        if not _is_ascii_path(local_path):
            src = _adb_ascii_copy(local_path)
        rc, out, err = _run_adb(["-s", serial, "install", "-r", src], timeout=timeout)
        text = (out + err).decode("utf-8", errors="replace").strip()
        ok = rc == 0 and "success" in text.lower()
        if not ok and not text:
            text = "install 无输出（可能失败）"
        return {"ok": ok, "output": text}
    finally:
        if src != local_path:
            try:
                os.remove(src)
            except OSError:
                pass


def adb_disconnect(serial: str):
    """断开 ADB 设备。"""
    _run_adb(["disconnect", serial], timeout=5)


def get_device_info(serial: str) -> dict:
    """通过 getprop 获取设备信息。"""
    info = {}
    props = [
        ("ro.product.model", "model"),
        ("ro.build.version.release", "android_version"),
        ("ro.build.version.sdk", "sdk"),
        ("ro.product.brand", "brand"),
        ("ro.product.manufacturer", "manufacturer"),
    ]
    for prop, key in props:
        rc, out, _ = _run_adb(["-s", serial, "shell", "getprop", prop], timeout=5)
        val = out.decode("utf-8", errors="replace").strip() if isinstance(out, bytes) else out.strip()
        info[key] = val
    return info


def _screencap(serial: str) -> bytes | None:
    """执行 adb exec-out screencap -p，返回缩略图字节（JPEG）；失败返回 None。

    拿到的是设备原图（1080x1920 PNG，单张 300~700KB）。设备列表一屏就是几十上百张，
    原图既占后端缓存内存，也拖慢浏览器解码，所以这里统一压成缩略图再往上返回。
    """
    rc, out, err = _run_adb(["-s", serial, "exec-out", "screencap", "-p"], timeout=20)
    if rc != 0 or not out:
        return None
    # 确保是 PNG（有些设备会输出多余文本）
    if out[:4] != b"\x89PNG":
        idx = out.find(b"\x89PNG")
        if idx >= 0:
            out = out[idx:]
        else:
            return None
    return _shrink_screenshot(out)


# ---- 截图降质（缩略图）----
# 卡片缩略图的目标宽度：约 300p 足够看清界面，单张压到十几 KB
SCREENSHOT_MAX_WIDTH = int(os.environ.get("SCREENSHOT_MAX_WIDTH", "320"))
SCREENSHOT_JPEG_QUALITY = int(os.environ.get("SCREENSHOT_JPEG_QUALITY", "70"))


def _shrink_screenshot(png: bytes, max_w: int = 0, quality: int = 0) -> bytes:
    """把 adb 原图压成 JPEG 缩略图；Pillow 不可用或处理失败时原样返回。"""
    max_w = max_w or SCREENSHOT_MAX_WIDTH
    quality = quality or SCREENSHOT_JPEG_QUALITY
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - 缺少依赖时降级，不影响截图功能
        return png
    try:
        import io as _io
        im = Image.open(_io.BytesIO(png))
        im.load()
        w, h = im.size
        if w > max_w:
            try:
                resample = Image.Resampling.BILINEAR
            except AttributeError:  # Pillow < 9.1
                resample = Image.BILINEAR
            im = im.resize((max_w, max(1, round(h * max_w / w))), resample)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = _io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return png


# 同设备并发截图请求合并（serial -> {evt, result}），避免同一设备并发重复 adb 截图
_SCREENSHOT_INFLIGHT: dict = {}
_INFLIGHT_LOCK = threading.Lock()
# 截图在途请求并发保护：超过上限的请求直接走缓存/快速失败，避免线程无限积压
_SCREENSHOT_ACTIVE = 0
_SCREENSHOT_ACTIVE_LOCK = threading.Lock()
SCREENSHOT_MAX_CONCURRENT = 48


def try_acquire_screenshot_slot() -> bool:
    """尝试占用一个截图在途槽位；满则返回 False（快速失败）。"""
    global _SCREENSHOT_ACTIVE
    with _SCREENSHOT_ACTIVE_LOCK:
        if _SCREENSHOT_ACTIVE >= SCREENSHOT_MAX_CONCURRENT:
            return False
        _SCREENSHOT_ACTIVE += 1
        return True


def release_screenshot_slot():
    global _SCREENSHOT_ACTIVE
    with _SCREENSHOT_ACTIVE_LOCK:
        _SCREENSHOT_ACTIVE = max(0, _SCREENSHOT_ACTIVE - 1)


def get_screenshot(serial: str) -> bytes | None:
    """通过 adb exec-out screencap -p 获取设备截图（PNG 字节），带缓存、并发合并与限流。

    - 缓存：TTL 跟随系统设置（默认 5s），命中直接返回；
    - 合并：同一 serial 的并发请求共享一次 adb 截图结果，不重复执行；
    - 限流：全局在途请求超过上限（SCREENSHOT_MAX_CONCURRENT）时快速失败（返回 None）。
    """
    now = time.time()
    hit = _SCREENSHOT_CACHE.get(serial)
    if hit and now - hit[0] < _effective_shot_ttl():
        return hit[1]
    # 并发保护：超限时返回最近缓存兜底（避免线程无限排队；旧图对监控可接受）
    if not try_acquire_screenshot_slot():
        hit = _SCREENSHOT_CACHE.get(serial)
        return hit[1] if hit else None
    # 陈旧兜底开关：本次 adb 截图若失败，先用上一张顶上，
    # 避免卡片在公网设备偶发超时时直接显示"截图失败"。
    allow_stale = True
    try:
        # 同 serial 并发请求合并：只有一个作为 owner 去截图，其余等待结果
        with _INFLIGHT_LOCK:
            inf = _SCREENSHOT_INFLIGHT.get(serial)
            if inf is None:
                inf = {"evt": threading.Event(), "result": None}
                _SCREENSHOT_INFLIGHT[serial] = inf
                owner = True
            else:
                owner = False
        if not owner:
            inf["evt"].wait(timeout=25)
            return inf["result"]
        try:
            with _SCREENSHOT_SEM:
                # 截图前确保 adb 已连接，避免首屏并发首次 connect 失败
                _ensure_adb_connected(serial)
                img = _screencap(serial)
                if img is None:
                    # 失败重试一次：丢弃连接状态、重新 connect 后再截图
                    _forget_adb_connection(serial)
                    _ensure_adb_connected(serial)
                    img = _screencap(serial)
                if img is None and allow_stale:
                    # 这次没截到 → 退回上一张陈旧截图。
                    # 卡片缩略图对"实时"要求不高，一张旧图远好于"截图失败"。
                    stale = _SCREENSHOT_CACHE.get(serial)
                    img = stale[1] if stale else None
            result = img
            if result is not None:
                _SCREENSHOT_CACHE[serial] = (time.time(), result)
                # 简单清理：缓存项过多时清空（避免设备删除后残留）
                if len(_SCREENSHOT_CACHE) > 200:
                    _SCREENSHOT_CACHE.clear()
        finally:
            inf["result"] = result
            inf["evt"].set()
            with _INFLIGHT_LOCK:
                _SCREENSHOT_INFLIGHT.pop(serial, None)
        return result
    finally:
        release_screenshot_slot()


# 截图缓存与并发控制：30 台设备卡片轮询截图时避免 adb 截图风暴
_SCREENSHOT_CACHE: dict = {}
_SCREENSHOT_SEM = threading.BoundedSemaphore(8)
# 截图缓存 TTL 跟随系统设置（screenshot_interval，默认 5s），30 秒内只查一次设置避免高频 DB 读
_SHOT_TTL_VAL = 5.0
_SHOT_TTL_TS = 0.0

def _effective_shot_ttl() -> float:
    global _SHOT_TTL_VAL, _SHOT_TTL_TS
    now = time.time()
    if now - _SHOT_TTL_TS > 30:
        try:
            from .models import get_setting
            v = float(get_setting("screenshot_interval") or 5)
            # 下限 8s：公网设备一次 adb screencap 常常要十几秒，TTL 若跟着 5s 的
            # 前端轮询走，缓存必然每次都过期，等于没有缓存 → 请求全部打到 adb 上堆积。
            _SHOT_TTL_VAL = max(8.0, v)
        except Exception:
            pass
        _SHOT_TTL_TS = now
    return _SHOT_TTL_VAL


def get_screen_size(serial: str) -> tuple[int, int]:
    """获取设备屏幕物理分辨率 (width, height)。"""
    rc, out, _ = _run_adb(["-s", serial, "shell", "wm", "size"], timeout=5)
    if rc == 0 and out:
        text = out.decode("utf-8", errors="replace").strip()
        # "Physical size: 1080x1920"
        import re
        m = re.search(r"(\d+)x(\d+)", text)
        if m:
            return int(m.group(1)), int(m.group(2))
    return 1080, 1920  # 默认回退


def get_input_size(serial: str) -> tuple[int, int]:
    """设备**输入坐标系**的真实分辨率（触摸/滑动坐标要用这个）。

    和 get_screen_size 的差别在于方向：读 `wm size` 的 Physical size，再按
    `dumpsys display` 的 rotation 校正（横屏时交换宽高）。
    刻意不用 Override size —— redroid 的 Override 常是横屏但真实渲染是竖屏，
    照它算会把坐标算反。
    """
    w, h = get_screen_size(serial)
    try:
        rc, out, _ = _run_adb(
            ["-s", serial, "shell", "dumpsys", "display"], timeout=8)
        if rc == 0 and out:
            import re
            text = out.decode("utf-8", errors="replace")
            m = re.search(r"rotation\s*=\s*(\d)", text)
            rot = int(m.group(1)) if m else 0
            if rot in (1, 3):      # 横屏：宽高互换
                w, h = h, w
    except Exception:  # noqa: BLE001
        pass
    return w, h


def get_frame_size(serial: str) -> tuple[int, int]:
    """推流帧尺寸（video letterbox 用）。

    screenrecord 会按 STREAM_FRAME_SIZE 缩放输出，所以帧尺寸 ≠ 设备分辨率。
    前端必须区分这两个值，否则把帧尺寸当坐标用会让点击整体偏移
    （1080→720 就是 1.5 倍偏差，越往右下越明显）。
    """
    if STREAM_FRAME_SIZE:
        return STREAM_FRAME_SIZE
    return get_screen_size(serial)


# ADBKeyboard 包探测缓存（避免每次输入都查包）
_ADBKB_CHECK = {}


def _has_adbkeyboard(serial: str) -> bool:
    """检测设备是否已安装 ADBKeyboard（包名 com.android.adbkeyboard）。"""
    if serial not in _ADBKB_CHECK:
        try:
            adb_connect(serial)  # 确保已连接
            rc, out, _ = _run_adb(
                ["-s", serial, "shell", "pm list packages | grep -i adbkeyboard"], timeout=8)
            text = (out or b"").decode("latin-1", "ignore").lower()
            _ADBKB_CHECK[serial] = rc == 0 and "com.android.adbkeyboard" in text
        except Exception:  # noqa: BLE001
            _ADBKB_CHECK[serial] = False
    return _ADBKB_CHECK[serial]


def input_text(serial: str, text: str) -> dict:
    """向设备输入文字。

    优先级：
    0. ADBKeyboard 广播（支持中文，需设备已安装并启用 ADBKeyBoard 输入法）
    1. 纯 ASCII → input text 直接注入
    2. 中文 → 剪贴板粘贴（clipper / service call）回退
    """
    adb_connect(serial)  # 确保 ADB 已连接（连接会断，需自动重连）

    # 方案0: ADBKeyboard —— 直达当前焦点输入框，中文也可靠
    if _has_adbkeyboard(serial):
        escaped = text.replace("'", "'\\''")
        rc, _, err = _run_adb([
            "-s", serial, "shell",
            "am", "broadcast", "-a", "ADB_INPUT_TEXT", "--es", "msg", escaped,
        ], timeout=5)
        if rc == 0:
            return {"ok": True, "method": "adbkeyboard_broadcast", "text": text}

    # 纯 ASCII 直接 input text
    if all(ord(c) < 128 for c in text):
        safe = text.replace(" ", "%s").replace("'", r"\'").replace('"', r'\"')
        rc, out, err = _adb_shell(serial, f"input text \"{safe}\"")
        return {"ok": rc == 0, "method": "input_text", "text": text}

    # 非 ASCII（中文等）：用剪贴板粘贴方案
    # 方案1: Clipper 应用 (am broadcast -a clipper.set)
    import shlex
    escaped = text.replace("'", "'\\''")
    rc1, _, _ = _run_adb([
        "-s", serial, "shell",
        "am", "broadcast", "-a", "clipper.set", "-e", "text", escaped
    ], timeout=5)
    if rc1 == 0:
        # 发送粘贴键
        _adb_shell(serial, "input keyevent 279")  # KEYCODE_PASTE
        return {"ok": True, "method": "clipper_clipboard", "text": text}

    # 方案2: 用 service call 操作剪贴板（部分设备支持）
    # 将文字转成 UTF-16LE hex 用于 service call
    try:
        utf16 = text.encode("utf-16-le")
        hex_str = "".join(f"{b:02x}" for b in utf16)
        # service call clipboard 2 i32 1 i64 0 i64 0 s16 <hex>
        rc2, _, _ = _run_adb([
            "-s", serial, "shell",
            "service", "call", "clipboard", "2", "i32", "1", "i64", "0", "i64", "0", "s16", hex_str
        ], timeout=5)
        if rc2 == 0:
            _adb_shell(serial, "input keyevent 279")
            return {"ok": True, "method": "service_clipboard", "text": text}
    except Exception:
        pass

    # 方案3: 回退到 input text（可能只输出问号，但至少不报错）
    safe = text.replace(" ", "%s")
    _adb_shell(serial, f"input text \"{safe}\"")
    return {"ok": False, "method": "fallback", "text": text,
            "error": "设备不支持中文剪贴板输入，建议安装 Clipper 或 ADBKeyboard"}


def _adb_shell(serial: str, command: str, timeout: int = 10) -> tuple:
    """执行 adb shell 命令。"""
    return _run_adb(["-s", serial, "shell"] + command.split(), timeout=timeout)


def _find_accel_event_node(shell_out: str) -> str:
    """从 getevent -pl 输出解析加速度传感器 input 节点（name 含 accel/gsensor/sensor 且带 ABS_X/ABS_Y/ABS_Z）。"""
    node, name, flags = "", "", set()
    for line in shell_out.splitlines():
        st = line.strip()
        if st.startswith("add device"):
            if node and {"ABS_X", "ABS_Y", "ABS_Z"} <= flags and (
                    "accel" in name.lower() or "gsensor" in name.lower() or "sensor" in name.lower()):
                return node
            m = re.search(r"/dev/input/event(\d+)", st)
            node = "/dev/input/event" + m.group(1) if m else ""
            name, flags = "", set()
        elif st.startswith("name:"):
            name = st.split(":", 1)[1].strip().strip('"')
        elif "ABS_X" in st:
            flags.add("ABS_X")
        elif "ABS_Y" in st:
            flags.add("ABS_Y")
        elif "ABS_Z" in st:
            flags.add("ABS_Z")
    if node and {"ABS_X", "ABS_Y", "ABS_Z"} <= flags and (
            "accel" in name.lower() or "gsensor" in name.lower() or "sensor" in name.lower()):
        return node
    return ""


def _shake_seq(node: str) -> str:
    """构造模拟摇一摇的 sendevent 序列（EV_ABS 三轴快速变化 + SYN_REPORT，多组）。"""
    groups = [
        ((57, 3000), (58, 2000), (59, 4000)),
        ((57, -2000), (58, -3000), (59, -1000)),
        ((57, 4000), (58, 1000), (59, -3000)),
        ((57, -3000), (58, 4000), (59, 2000)),
        ((57, 1500), (58, -2500), (59, 3500)),
    ]
    parts = []
    for g in groups:
        for code, val in g:
            parts.append("sendevent %s 3 %d %d" % (node, code, val))
        parts.append("sendevent %s 0 0 0" % node)
    return "; sleep 0.04; ".join(parts)


def _ssh_bridge_shell(host: str, serial: str, cmd: str, timeout: int = 25) -> tuple:
    """通过 SSH 到宿主执行 adb shell 命令（ssh_bridge 设备用），返回 (rc, out, err)。"""
    import paramiko
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, username="firefly", password="Passw0rd", timeout=8)
        _, stdout, stderr = ssh.exec_command("adb -s %s shell %s" % (serial, cmd), timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        ssh.close()
        return 0, out, err
    except Exception as e:
        return -1, "", str(e)


def install_apk(serial: str, apk_path: str) -> dict:
    """通过 adb 安装 APK 到指定设备（-r 覆盖安装）。"""
    rc, out, err = _run_adb(["-s", serial, "install", "-r", apk_path], timeout=180)
    ok = rc == 0
    detail = out.decode(errors="ignore").strip() or err.decode(errors="ignore").strip()
    return {"serial": serial, "ok": ok,
            "result": detail or ("installed" if ok else "install failed")}


# ---------------------------------------------------------------------------
# 统一接口
# ---------------------------------------------------------------------------
def create_device(backend: str, name: str, serial: str = None) -> dict:
    """创建设备。

    - simulator：生成 Mock 设备
    - redroid：如果指定 serial 则连接已有设备；否则分配端口并提示需在服务器启动容器
    """
    server = _server_ip()

    if backend == "simulator":
        port = _alloc_port()
        s = f"{server}:{port}"
        state = {
            "status": "running",
            "ip": server,
            "fingerprint": _fake_fp(),
        }
        _SIM_STATE[s] = state
        return {"serial": s, **state}

    elif backend == "redroid":
        created_profile = None
        # 如果指定了 serial，连接已有设备
        if serial:
            target = serial
            if ":" in target:
                adb_connect(target)
        else:
            # 自动创建：查找可用端口，在服务器上创建 redroid 容器
            if _ssh_enabled():
                from . import device_env
                port = find_available_adb_port()
                profile = device_env.generate_profile(port)
                result = create_redroid_container(port, profile=profile)
                if not result["ok"]:
                    return {"serial": "", "status": "error", "ip": _server_ip(),
                            "fingerprint": "", "error": result["error"]}
                target = result["serial"]
                created_profile = result.get("profile") or profile
            else:
                # SSH 未配置，回退到自动发现已有设备
                devices = list_adb_devices()
                online = [d for d in devices if d["status"] == "device"]
                if online:
                    target = online[0]["serial"]
                else:
                    return {"serial": "", "status": "error", "ip": _server_ip(),
                            "fingerprint": "", "error": "无可用 ADB 设备，请先连接或配置 SSH"}

        # 获取设备信息
        info = get_device_info(target)
        if created_profile:
            fp = json.dumps(created_profile, ensure_ascii=False)
        else:
            fp = json.dumps({
                "model": info.get("model", "redroid"),
                "android_version": info.get("android_version", "12"),
                "brand": info.get("brand", ""),
                "manufacturer": info.get("manufacturer", ""),
            }, ensure_ascii=False)

        return {
            "serial": target,
            "status": "running",
            "ip": _server_ip(),
            "fingerprint": fp,
        }

    raise ValueError(f"未知后端: {backend}")


def recreate_redroid_container(port: int, profile: dict | None = None) -> dict:
    """用新的设备指纹重建容器：保留 ~/redroid-data/<port>（用户数据不丢）。

    注意：会短暂重启这台云手机，调用方应在维护窗口或用户确认后使用。
    """
    if not _ssh_enabled():
        return {"ok": False, "error": "SSH 未配置"}
    cfg = _ssh_config()
    client = None
    try:
        client = _ssh_connect()
        container_name = _find_container_by_port(port) or f"{cfg['prefix']}-{port}"
        _ssh_exec(client, f"docker rm -f {container_name}", timeout=60)
    finally:
        if client:
            client.close()
    return create_redroid_container(port, profile=profile)


def delete_device(backend: str, serial: str):
    """删除设备。simulator 清理内存态，redroid 断开 ADB 并删除服务器容器。"""
    _SIM_STATE.pop(serial, None)
    try:
        port = int(serial.split(":")[-1])
        _free_port(port)
    except (ValueError, IndexError):
        pass
    if backend == "redroid" and ":" in serial:
        # 先断开 ADB
        adb_disconnect(serial)
        # 如果配置了 SSH，删除服务器上的容器
        if _ssh_enabled():
            try:
                port = int(serial.split(":")[-1])
                delete_redroid_container(port)
            except (ValueError, IndexError):
                pass


# ---------------------------------------------------------------------------
# 云手机轮次运行（24h 分 N 轮，每轮结束销毁重建）
# ---------------------------------------------------------------------------
def get_rotation_config():
    """获取轮次运行配置（单行 id=1，不存在则初始化默认值）。"""
    from .extensions import db
    from .models import RotationConfig
    cfg = db.session.get(RotationConfig, 1)
    if cfg is None:
        cfg = RotationConfig(id=1, enabled=False, rounds=4)
        db.session.add(cfg)
        db.session.commit()
        db.session.refresh(cfg)
    return cfg


def rotate_redroid_devices(target_count=None) -> dict:
    """销毁所有 redroid 容器，并按配置数量（devices_per_round）重新创建。

    用于轮次运行模式：每轮结束调用，相当于整批云手机「重置」。
    target_count 不传时使用轮次配置里的 devices_per_round。
    """
    from .extensions import db
    from .models import Device
    if target_count is None:
        cfg = get_rotation_config()
        target_count = cfg.devices_per_round or 2
    target_count = int(target_count)

    devices = db.session.query(Device).filter_by(backend="redroid").all()

    # 1) 销毁旧容器 + 删除 DB 记录
    destroyed = 0
    for d in devices:
        try:
            port = int(d.serial.split(":")[-1])
            delete_redroid_container(port)
        except Exception:
            pass
        try:
            adb_disconnect(d.serial)
        except Exception:
            pass
        db.session.delete(d)
        destroyed += 1
    db.session.commit()

    # 2) 按配置数量重新创建（redroid-1 .. redroid-N）
    created = 0
    failed = 0
    for i in range(target_count):
        name = f"redroid-{i+1}"
        try:
            state = create_device("redroid", name)
            if state.get("serial"):
                nd = Device(
                    name=name, status=state["status"], serial=state["serial"],
                    backend="redroid", ip=state.get("ip", ""),
                    fingerprint=state.get("fingerprint", ""),
                )
                db.session.add(nd)
                created += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    db.session.commit()
    return {"ok": True, "destroyed": destroyed, "created": created,
            "failed": failed, "target": target_count}


def control_device(backend: str, serial: str, action: str, payload: dict) -> dict:
    """单台设备操控。

    simulator：记录日志；
    redroid：通过 ADB 执行真实命令。
    """
    if backend == "simulator":
        return {"serial": serial, "action": action, "ok": True,
                "result": f"[simulator] executed {action} on {serial}"}

    if backend == "redroid":
        # 用带缓存的 _ensure_adb_connected（首次 connect 后保持），避免每次控制
        # 都重新 adb connect 导致公网设备握手不稳定 → 控制超时/失败
        _ensure_adb_connected(serial)
        try:
            if action == "tap":
                x = payload.get("x", 500)
                y = payload.get("y", 500)
                rc, out, err = _adb_shell(serial, f"input tap {x} {y}")
                return {"serial": serial, "action": action, "ok": rc == 0,
                        "result": f"tap ({x}, {y})"}

            elif action == "swipe":
                x1, y1 = payload.get("x1", 100), payload.get("y1", 500)
                x2, y2 = payload.get("x2", 400), payload.get("y2", 500)
                duration = payload.get("duration", 300)
                _adb_shell(serial, f"input swipe {x1} {y1} {x2} {y2} {duration}")
                return {"serial": serial, "action": action, "ok": True,
                        "result": f"swipe ({x1},{y1}) -> ({x2},{y2})"}

            elif action == "text":
                text = payload.get("text", "")
                result = input_text(serial, text)
                return {"serial": serial, "action": action, **result}

            elif action == "screen_size":
                # width/height  = **输入坐标系**（触摸坐标用，横屏已校正）
                # frame_*       = 推流帧尺寸（video letterbox 用）
                # 推流做了缩放时二者不同，前端不能混用。
                iw, ih = get_input_size(serial)
                fw, fh = get_frame_size(serial)
                return {"serial": serial, "action": action, "ok": True,
                        "width": iw, "height": ih,
                        "frame_width": fw, "frame_height": fh,
                        "scale": round(iw / fw, 4) if fw else 1.0}

            elif action == "rotate":
                # 让云手机**自己**转（不是只转本地画面）。
                # rotation: 0/1/2/3 → 0° / 90° / 180° / 270°；"auto" → 打开自动旋转。
                # 注意：必须先关 accelerometer_rotation，否则系统会被重力传感器盖回去。
                def _s(v):
                    # _adb_shell 返回 bytes，直接塞进 jsonify 会 500
                    if isinstance(v, (bytes, bytearray)):
                        return v.decode("utf-8", "replace")
                    return v or ""

                raw = payload.get("rotation")
                if raw in (None, ""):
                    return {"serial": serial, "action": action, "ok": False,
                            "error": "缺少 rotation 参数"}
                if str(raw).lower() == "auto":
                    _adb_shell(serial, "settings put system accelerometer_rotation 1")
                    return {"serial": serial, "action": action, "ok": True, "rotation": "auto"}
                try:
                    n = int(raw) % 4
                except (TypeError, ValueError):
                    return {"serial": serial, "action": action, "ok": False,
                            "error": "rotation 必须是 0-3（或 auto）"}
                _adb_shell(serial, "settings put system accelerometer_rotation 0")
                rc, out, err = _adb_shell(serial, "settings put system user_rotation %d" % n)
                if rc != 0:
                    return {"serial": serial, "action": action, "ok": False,
                            "error": _s(err).strip() or _s(out).strip() or "设置 user_rotation 失败"}
                # 读回确认（有些 ROM 会忽略写入）
                _rc2, cur, _e2 = _adb_shell(serial, "settings get system user_rotation")
                applied = _s(cur).strip()
                return {"serial": serial, "action": action, "ok": applied == str(n),
                        "rotation": n, "applied": applied,
                        "error": "" if applied == str(n) else "设备未接受该旋转角度"}

            elif action == "key":
                keycode = payload.get("keycode", 3)  # 默认 HOME
                rc, out, err = _adb_shell(serial, f"input keyevent {keycode}")
                if rc != 0:
                    # 以前不管成败都回 ok=True，前端看不出按键没生效；
                    # 这里把 adb 的失败原因原样抛给调用方，界面能提示出来。
                    detail = (err or out or b"").decode("utf-8", "replace").strip()
                    raise RuntimeError(detail[:160] or f"按键 {keycode} 执行失败（设备可能已离线）")
                return {"serial": serial, "action": action, "ok": True,
                        "result": f"keyevent {keycode}"}

            elif action == "open_url":
                url = payload.get("url", "https://www.baidu.com")
                _adb_shell(serial, f"am start -a android.intent.action.VIEW -d {url}")
                return {"serial": serial, "action": action, "ok": True,
                        "result": f"open {url}"}

            elif action == "install":
                # 需要本地 apk 路径，暂返回提示
                return {"serial": serial, "action": action, "ok": False,
                        "result": "install 需要指定 apk 路径"}

            elif action == "wait":
                seconds = payload.get("seconds", 1)
                time.sleep(min(seconds, 10))
                return {"serial": serial, "action": action, "ok": True,
                        "result": f"waited {seconds}s"}

            elif action == "sequence":
                steps = payload.get("steps", [])
                results = []
                for step in steps:
                    a = step.get("action")
                    if a:
                        r = control_device(backend, serial, a, step)
                        results.append(r)
                return {"serial": serial, "action": action, "ok": True,
                        "result": f"executed {len(results)} steps", "details": results}

            elif action == "screenshot":
                img = get_screenshot(serial)
                return {"serial": serial, "action": action, "ok": img is not None,
                        "result": "screenshot captured" if img else "failed"}

            else:
                return {"serial": serial, "action": action, "ok": False,
                        "result": f"unknown action: {action}"}

        except Exception as e:
            return {"serial": serial, "action": action, "ok": False,
                    "result": f"error: {str(e)}"}

    raise NotImplementedError(f"backend {backend} control 未实现")


# ---------------------------------------------------------------------------
# SSH 远程管理 redroid Docker 容器
# ---------------------------------------------------------------------------
def _ssh_config() -> dict:
    """获取 SSH 配置。"""
    try:
        cfg = current_app.config
    except RuntimeError:
        cfg = os.environ
    return {
        "host": cfg.get("SSH_HOST", ""),
        "port": int(cfg.get("SSH_PORT", 22)),
        "username": cfg.get("SSH_USERNAME", ""),
        "password": cfg.get("SSH_PASSWORD", ""),
        "key_file": cfg.get("SSH_KEY_FILE", ""),
        "image": cfg.get("REDROID_IMAGE", "redroid/redroid:12.0.0_latest"),
        "prefix": cfg.get("REDROID_CONTAINER_PREFIX", "redroid"),
    }


def _ssh_enabled() -> bool:
    """检查 SSH 是否配置可用。"""
    cfg = _ssh_config()
    return bool(cfg["host"] and cfg["username"])


def _ssh_connect():
    """建立 SSH 连接，返回 paramiko.SSHClient。"""
    import paramiko
    cfg = _ssh_config()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = {
        "hostname": cfg["host"],
        "port": cfg["port"],
        "username": cfg["username"],
        "timeout": 10,
    }
    if cfg["key_file"] and os.path.isfile(cfg["key_file"]):
        connect_kwargs["key_filename"] = cfg["key_file"]
    elif cfg["password"]:
        connect_kwargs["password"] = cfg["password"]
    client.connect(**connect_kwargs)
    return client


_shared_ssh = {"client": None}


def _ssh_shared_client():
    """复用一条 SSH 连接（诊断场景会连续执行多条命令）。"""
    cli = _shared_ssh.get("client")
    try:
        if cli is not None and cli.get_transport() and cli.get_transport().is_active():
            return cli
    except Exception:  # noqa: BLE001
        pass
    cli = _ssh_connect()
    _shared_ssh["client"] = cli
    return cli


def _ssh_exec(client, command: str, timeout: int = 30) -> tuple:
    """执行远程命令，返回 (returncode, stdout, stderr)。"""
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


def list_remote_containers() -> list[dict]:
    """列出服务器上所有 redroid 容器。"""
    if not _ssh_enabled():
        return []
    cfg = _ssh_config()
    client = None
    try:
        client = _ssh_connect()
        cmd = f"docker ps -a --filter name={cfg['prefix']} --format '{{{{.Names}}}}|{{{{.Status}}}}|{{{{.Ports}}}}'"
        rc, out, err = _ssh_exec(client, cmd)
        containers = []
        if rc == 0:
            for line in out.strip().splitlines():
                parts = line.split("|")
                if len(parts) >= 1:
                    name = parts[0].strip()
                    # 从容器名提取端口
                    try:
                        port = int(name.split("-")[-1])
                    except (ValueError, IndexError):
                        port = 0
                    containers.append({
                        "name": name,
                        "status": parts[1].strip() if len(parts) > 1 else "",
                        "ports": parts[2].strip() if len(parts) > 2 else "",
                        "port": port,
                    })
        return containers
    except Exception as e:
        current_app.logger.warning(f"列出远程容器失败: {e}")
        return []
    finally:
        if client:
            client.close()


def find_available_adb_port() -> int:
    """查找服务器上可用的 ADB 端口（从 15555 开始递增）。"""
    used = set()
    # 收集已使用的端口
    for d in list_adb_devices():
        try:
            used.add(int(d["serial"].split(":")[-1]))
        except (ValueError, IndexError):
            pass
    for c in list_remote_containers():
        if c["port"]:
            used.add(c["port"])
    port = 15555
    while port in used:
        port += 1
    return port


def _adb_host() -> str:
    """本地 adb 可达的主机。

    新服务器容器端口绑定 127.0.0.1，需本地 SSH 隧道（adb_tunnel.py）把端口
    转发到 localhost。开启 SSH_TUNNEL_ENABLED 时设备 serial 用 localhost:port，
    否则直接用服务器 IP:port。
    """
    import os
    val = None
    try:
        val = current_app.config.get("SSH_TUNNEL_ENABLED", None)
    except RuntimeError:
        val = None
    if val is None:
        val = os.environ.get("SSH_TUNNEL_ENABLED", "false")
    if str(val).strip().lower() in ("1", "true", "yes"):
        return "localhost"
    return _server_ip()



def list_server_tunnel_ports() -> dict:
    """读取服务器 redroid-platform-tunnel.service 的反向隧道映射。

    返回 {容器端口: 对外隧道端口}。用于把容器内部 127.0.0.1 绑定的 adb 端口
    映射为公网可达的隧道端口（例如 {6000: 5901, 6600: 5902, ...}）。
    无隧道服务时返回空 dict（兼容旧服务器直连容器端口）。
    """
    if not _ssh_enabled():
        return {}
    client = None
    try:
        client = _ssh_connect()
        rc, out, err = _ssh_exec(client, "cat /etc/systemd/system/redroid-platform-tunnel.service")
        mapping = {}
        if rc == 0:
            for m in re.finditer(r"-R\s+0\.0\.0\.0:(\d+):127\.0\.0\.1:(\d+)", out):
                tunnel_port, container_port = int(m.group(1)), int(m.group(2))
                mapping[container_port] = tunnel_port
        return mapping
    except Exception:
        return {}
    finally:
        if client:
            client.close()


def list_server_redroid_ports() -> list:
    """扫描服务器上运行中的 redroid 容器，返回其对外 ADB 端口列表。

    以 docker 容器真实运行状态为准（比易失的 adb 连接更可靠），供设备同步使用。
    """
    ports = set()
    for c in list_remote_containers():
        if not c["status"].startswith("Up"):
            continue
        for seg in c["ports"].split(","):
            seg = seg.strip()
            # 兼容 0.0.0.0:PORT 与 127.0.0.1:PORT 两种绑定
            if "->" not in seg:
                continue
            host_part = seg.split("->")[0].strip()
            if ":" in host_part:
                host_part = host_part.rsplit(":", 1)[-1]
            try:
                ports.add(int(host_part))
            except ValueError:
                pass
    return sorted(ports)


def _find_container_by_port(port: int) -> str | None:
    """在服务器上按 ADB 端口查找容器名（兼容 redroid1/redroid2 与 redroid-15555 命名）。"""
    for c in list_remote_containers():
        # ports 字段形如 "0.0.0.0:15555->5555/tcp, [::]:15555->5555/tcp"
        host_ports = set()
        for seg in c["ports"].split(","):
            seg = seg.strip()
            if "->" not in seg:
                continue
            host_part = seg.split("->")[0].strip()
            if ":" in host_part:
                host_part = host_part.rsplit(":", 1)[-1]
            try:
                host_ports.add(int(host_part))
            except ValueError:
                pass
        if port in host_ports:
            return c["name"]
    return None


def _deploy_scrcpy_server(client, port: int, retries: int = 3, delay: float = 6) -> None:
    """在服务器上为新容器部署 scrcpy server（确保 ADB 连接 + push jar + setsid 启动）。

    幂等（jar 已存在则直接启动）；带重试：ADB 未就绪时自动等待重试，
    保证新创建的云手机首次预览即有画面。部署最终失败不阻断主流程。
    """
    jar = current_app.config.get("SCRCPY_SERVER_JAR", "")
    ver = current_app.config.get("SCRCPY_SERVER_VERSION", "1.19-ws8")
    sport = int(current_app.config.get("SCRCPY_SERVER_PORT", 8886))
    if not jar:
        return
    for attempt in range(max(1, retries)):
        try:
            # 每次先确保 ADB 已连接（容器冷启动可能较慢）
            _ssh_exec(client, f"adb connect 127.0.0.1:{port}", timeout=20)
            rc, out, err = _ssh_exec(
                client,
                f"adb -s 127.0.0.1:{port} push {jar} /data/local/tmp/scrcpy-server.jar",
                timeout=60,
            )
            if rc != 0:
                time.sleep(delay)
                continue
            _ssh_exec(
                client,
                f"adb -s 127.0.0.1:{port} shell 'CLASSPATH=/data/local/tmp/scrcpy-server.jar "
                f"setsid nohup app_process / com.genymobile.scrcpy.Server {ver} web ERROR {sport} true >/dev/null 2>&1 &'",
                timeout=30,
            )
            return
        except Exception:
            time.sleep(delay)


def _setup_default_apps(serial: str) -> dict:
    """新容器初始化：安装 iLauncher / x浏览器，隐藏 WebViewShell，设置默认启动器与默认浏览器。

    依赖 backend/resources/apk/ 下的 ilauncher.apk、xbrowser.apk。
    """
    import shutil
    import time as _t
    results: dict = {"enabled": True}
    if not current_app.config.get("REDROID_SETUP_ENABLED", True):
        return {"enabled": False}
    apk_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources", "apk")
    launcher_pkg = current_app.config.get("DEFAULT_LAUNCHER_PKG", "com.launcheros15.ilauncher")
    browser_pkg = current_app.config.get("DEFAULT_BROWSER_PKG", "com.mmbox.xbrowser")
    hide_pkg = current_app.config.get("HIDE_WEBVIEW_SHELL_PKG", "org.chromium.webview_shell")

    def is_installed(pkg: str) -> bool:
        rc, out, err = _adb_shell(serial, f"pm path {pkg}", timeout=15)
        return "package:" in out.decode(errors="ignore")

    # 1) 安装 iLauncher / x浏览器（已装则跳过）
    for fname, pkg in (("ilauncher.apk", launcher_pkg), ("xbrowser.apk", browser_pkg)):
        apk = os.path.join(apk_dir, fname)
        if not os.path.isfile(apk):
            results[fname] = "apk-missing"
            continue
        if is_installed(pkg):
            results[fname] = "already"
            continue
        tmp = _adb_ascii_copy(apk)
        try:
            rc, out, err = _run_adb(["-s", serial, "install", "-r", tmp], timeout=180)
            results[fname] = "ok" if rc == 0 else f"fail:{err.decode(errors='ignore')[:80]}"
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass

    # 2) 隐藏 WebViewShell
    if hide_pkg and is_installed(hide_pkg):
        rc, out, err = _adb_shell(serial, f"pm disable-user --user 0 {hide_pkg}", timeout=15)
        results["hide_webview"] = "ok" if rc == 0 else f"fail:{err.decode(errors='ignore')[:60]}"

    # 3) 设置默认启动器 / 默认浏览器（应用刚装完，稍等包管理刷新）
    _t.sleep(2)
    if is_installed(launcher_pkg):
        rc, out, err = _adb_shell(serial, f"cmd role add-role-holder --user 0 android.app.role.HOME {launcher_pkg}", timeout=20)
        results["home"] = "ok" if rc == 0 else f"fail:{err.decode(errors='ignore')[:60]}"
    if is_installed(browser_pkg):
        rc, out, err = _adb_shell(serial, f"cmd role add-role-holder --user 0 android.app.role.BROWSER {browser_pkg}", timeout=20)
        results["browser"] = "ok" if rc == 0 else f"fail:{err.decode(errors='ignore')[:60]}"

    # 4) 点亮默认启动器
    _adb_shell(serial, "am start -a android.intent.action.MAIN -c android.intent.category.HOME", timeout=15)
    return results


def create_redroid_container(port: int, profile: dict | None = None) -> dict:
    """在服务器上创建并启动 redroid 容器。

    profile：设备指纹（机型号/厂商/fingerprint/序列号），不传则按端口生成一套，
    启动参数里覆盖 ro.product.*，让容器对外是一部真实机型而不是 "redroid"。
    """
    from . import device_env

    if not _ssh_enabled():
        return {"ok": False, "error": "SSH 未配置"}
    if profile is None:
        profile = device_env.generate_profile(port)
    _serial_no = str(profile.get("serial") or f"SN{port}")
    cfg = _ssh_config()
    client = None
    try:
        client = _ssh_connect()
        container_name = _find_container_by_port(port) or f"{cfg['prefix']}-{port}"

        # 检查容器是否已存在
        rc, out, _ = _ssh_exec(client, f"docker inspect {container_name} >/dev/null 2>&1 && echo EXISTS || echo NOTFOUND")
        if "EXISTS" in out:
            # 容器已存在，启动它
            _ssh_exec(client, f"docker start {container_name}")
        else:
            # 创建新容器
            # 以第二台云手机（redroid_2/5557）为模板：720P/dpi320 流畅优先 + Pixel 6 伪装
            _gpu = current_app.config.get("REDROID_GPU_MODE", "guest")
            _w = current_app.config.get("REDROID_WIDTH", "720")
            _h = current_app.config.get("REDROID_HEIGHT", "1280")
            _dpi = current_app.config.get("REDROID_DPI", "320")
            _fps = str(current_app.config.get("REDROID_FPS", "") or "")
            _nb = str(current_app.config.get("REDROID_NATIVE_BRIDGE", "") or "")
            _brand = str(current_app.config.get("REDROID_PRODUCT_BRAND", "") or "")
            _manu = str(current_app.config.get("REDROID_PRODUCT_MANUFACTURER", "") or "")
            _model = str(current_app.config.get("REDROID_PRODUCT_MODEL", "") or "")
            _pname = str(current_app.config.get("REDROID_PRODUCT_NAME", "") or "")
            _locale = str(current_app.config.get("REDROID_LOCALE", "") or "")
            _env_args = device_env.docker_args(profile)
            docker_cmd = (
                f"docker run -itd --privileged "
                f"--name {container_name} "
                f"-p {port}:5555 "
                f"-v ~/redroid-data/{port}:/data "
                f"{cfg['image']} "
                f"androidboot.redroid_gpu_mode={_gpu} "
                f"androidboot.redroid_width={_w} "
                f"androidboot.redroid_height={_h} "
                f"androidboot.redroid_dpi={_dpi} "
                + (f"androidboot.redroid_fps={_fps} " if _fps else "")
                + (f"androidboot.redroid_root_props=ro.product.locale={_locale} " if _locale else "")
                + (_env_args + " " if _env_args else "")
                + (f"androidboot.native_bridge={_nb}" if _nb else "")
            )
            rc, out, err = _ssh_exec(client, docker_cmd, timeout=60)
            if rc != 0:
                return {"ok": False, "error": f"docker run 失败: {err.strip() or out.strip()}"}

        # 等待 ADB 就绪（最多 30 秒）
        server = _server_ip()
        serial = f"{server}:{port}"
        # 等待 ADB 就绪（最多 60 秒，每次迭代重连，兼容容器冷启动慢的情况）
        for _ in range(60):
            adb_connect(serial)
            devices = list_adb_devices()
            if any(d["serial"] == serial and d["status"] == "device" for d in devices):
                break
            time.sleep(1)


        # 自动部署 scrcpy server（保证视频投屏可用、画面一致）
        _deploy_scrcpy_server(client, port)

        # 新容器初始化：安装 iLauncher/x浏览器、隐藏 WebViewShell、设置默认启动器/浏览器
        _setup_default_apps(serial)

        # 时区 / 语言（persist 属性只能运行时设置）
        try:
            device_env.runtime_setup(serial, profile)
        except Exception as e:  # noqa: BLE001
            print(f"[device_env] 时区设置失败 {serial}: {e}", flush=True)

        return {"ok": True, "container": container_name, "port": port,
                "serial": serial, "profile": profile}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        if client:
            client.close()


def delete_redroid_container(port: int) -> dict:
    """在服务器上停止并删除 redroid 容器。"""
    if not _ssh_enabled():
        return {"ok": False, "error": "SSH 未配置"}
    cfg = _ssh_config()
    client = None
    try:
        client = _ssh_connect()
        container_name = _find_container_by_port(port) or f"{cfg['prefix']}-{port}"

        # 断开 ADB
        server = _server_ip()
        serial = f"{server}:{port}"
        adb_disconnect(serial)

        # 停止并删除容器
        rc, out, err = _ssh_exec(client, f"docker rm -f {container_name}", timeout=30)
        if rc != 0 and "No such container" not in err:
            return {"ok": False, "error": f"docker rm 失败: {err.strip() or out.strip()}"}

        return {"ok": True, "container": container_name}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        if client:
            client.close()


def container_power(port: int, action: str) -> dict:
    """控制 redroid 容器电源状态。

    action: start | stop | restart
    """
    if not _ssh_enabled():
        return {"ok": False, "error": "SSH 未配置"}
    cfg = _ssh_config()
    client = None
    try:
        client = _ssh_connect()
        container_name = _find_container_by_port(port) or f"{cfg['prefix']}-{port}"
        server = _server_ip()
        serial = f"{server}:{port}"

        if action == "start":
            rc, out, err = _ssh_exec(client, f"docker start {container_name}", timeout=30)
            if rc != 0:
                return {"ok": False, "error": f"开机失败: {err.strip() or out.strip()}"}
            # 等待 ADB 就绪
            adb_connect(serial)
            for _ in range(30):
                devices = list_adb_devices()
                if any(d["serial"] == serial and d["status"] == "device" for d in devices):
                    break
                time.sleep(1)

            # 自动部署 scrcpy server
            _deploy_scrcpy_server(client, port)
            return {"ok": True, "action": "start", "container": container_name}

        elif action == "stop":
            rc, out, err = _ssh_exec(client, f"docker stop {container_name}", timeout=30)
            if rc != 0:
                return {"ok": False, "error": f"关机失败: {err.strip() or out.strip()}"}
            adb_disconnect(serial)
            return {"ok": True, "action": "stop", "container": container_name}

        elif action == "restart":
            rc, out, err = _ssh_exec(client, f"docker restart {container_name}", timeout=60)
            if rc != 0:
                return {"ok": False, "error": f"重启失败: {err.strip() or out.strip()}"}
            # 等待 ADB 就绪
            adb_connect(serial)
            for _ in range(30):
                devices = list_adb_devices()
                if any(d["serial"] == serial and d["status"] == "device" for d in devices):
                    break
                time.sleep(1)

            # 自动部署 scrcpy server
            _deploy_scrcpy_server(client, port)
            return {"ok": True, "action": "restart", "container": container_name}

        return {"ok": False, "error": f"未知操作: {action}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        if client:
            client.close()


# ---------------------------------------------------------------------------
# VPC 出口线路 / SOCKS5 sidecar（来自 new-vpc 分支合并）
# ---------------------------------------------------------------------------
def _vpc_sidecar_name(port: int, prefix: str = "") -> str:
    prefix = prefix or _ssh_config().get("prefix") or "redroid"
    return f"{prefix}-vpc-{int(port)}"


def _vpc_proxy_url(host: str, port: int, username: str = "", password: str = "") -> str:
    from urllib.parse import quote
    user = (username or "").strip()
    pwd = password or ""
    if user:
        return f"socks5://{quote(user, safe='')}:{quote(pwd, safe='')}@{host}:{int(port)}"
    return f"socks5://{host}:{int(port)}"


def _vpc_curl_proxy(host: str, port: int, username: str = "", password: str = "") -> str:
    from urllib.parse import quote
    user = (username or "").strip()
    pwd = password or ""
    if user:
        return f"socks5h://{quote(user, safe='')}:{quote(pwd, safe='')}@{host}:{int(port)}"
    return f"socks5h://{host}:{int(port)}"


def ensure_dev_socks5() -> dict:
    """在云手机服务器上启动仅监听 127.0.0.1 的测试 SOCKS5（无账号）。"""
    if not _ssh_enabled():
        return {"ok": False, "error": "SSH 未配置"}
    try:
        image = current_app.config.get("VPC_DEV_SOCKS_IMAGE") or "serjs/go-socks5-proxy"
        port = int(current_app.config.get("VPC_DEV_SOCKS_PORT") or 1080)
    except RuntimeError:
        image = os.environ.get("VPC_DEV_SOCKS_IMAGE", "serjs/go-socks5-proxy")
        port = int(os.environ.get("VPC_DEV_SOCKS_PORT", "1080"))
    name = "vpc-dev-socks5"
    client = None
    try:
        client = _ssh_connect()
        rc, out, _ = _ssh_exec(
            client,
            f"docker inspect -f '{{{{.State.Running}}}}' {name} 2>/dev/null || true",
        )
        running = (out or "").strip().lower() == "true"
        if not running:
            _ssh_exec(client, f"docker rm -f {name}", timeout=20)
            run_cmd = (
                f"docker run -d --name {name} --restart unless-stopped "
                f"-p 127.0.0.1:{int(port)}:1080 {image}"
            )
            rc, out, err = _ssh_exec(client, run_cmd, timeout=120)
            if rc != 0:
                return {"ok": False, "error": (err or out or "启动测试 SOCKS5 失败").strip()[:240]}
        return {"ok": True, "host": "127.0.0.1", "port": int(port), "container": name}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    finally:
        if client:
            client.close()


def test_socks5_line(host: str, port: int, username: str = "", password: str = "") -> dict:
    """在云手机服务器上测试 SOCKS5，返回出口 IP。"""
    if not _ssh_enabled():
        return {"ok": False, "error": "SSH 未配置"}
    check_url = "https://ifconfig.me/ip"
    try:
        check_url = current_app.config.get("VPC_EGRESS_CHECK_URL") or check_url
    except RuntimeError:
        check_url = os.environ.get("VPC_EGRESS_CHECK_URL", check_url)
    proxy = _vpc_curl_proxy(host, port, username, password)
    client = None
    try:
        import shlex
        client = _ssh_connect()
        cmd = f"curl -sS --max-time 15 -x {shlex.quote(proxy)} {shlex.quote(check_url)}"
        rc, out, err = _ssh_exec(client, cmd, timeout=20)
        ip = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else ""
        if rc == 0 and ip and ip.count(".") == 3:
            return {"ok": True, "egress_ip": ip}
        detail = (err or out or "测试失败").strip()[:240]
        return {"ok": False, "error": detail or "SOCKS5 测试失败"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    finally:
        if client:
            client.close()


def apply_vpc_sidecar(port: int, host: str, socks_port: int,
                      username: str = "", password: str = "") -> dict:
    """给指定 ADB 端口的 redroid 容器挂 SOCKS5 sidecar。"""
    if not _ssh_enabled():
        return {"ok": False, "error": "SSH 未配置"}
    cfg = _ssh_config()
    try:
        image = current_app.config.get("VPC_SIDECAR_IMAGE") or "xjasonlyu/tun2socks:v2.5.2"
    except RuntimeError:
        image = os.environ.get("VPC_SIDECAR_IMAGE", "xjasonlyu/tun2socks:v2.5.2")
    redroid = _find_container_by_port(port) or f"{cfg['prefix']}-{port}"
    sidecar = _vpc_sidecar_name(port, cfg["prefix"])
    proxy = _vpc_proxy_url(host, socks_port, username, password)
    client = None
    try:
        client = _ssh_connect()
        rc, out, _ = _ssh_exec(
            client,
            f"docker inspect {redroid} >/dev/null 2>&1 && echo EXISTS || echo NOTFOUND",
        )
        if "EXISTS" not in (out or ""):
            return {"ok": False, "error": f"云机容器不存在: {redroid}"}
        _ssh_exec(client, f"docker rm -f {sidecar}", timeout=20)
        run_cmd = (
            f"docker run -d --privileged --restart unless-stopped "
            f"--name {sidecar} "
            f"--network container:{redroid} "
            f"{image} "
            f"-device tun:// "
            f"-proxy {proxy}"
        )
        rc, out, err = _ssh_exec(client, run_cmd, timeout=120)
        if rc != 0:
            return {"ok": False, "error": (err or out or "sidecar 启动失败").strip()[:240]}
        return {"ok": True, "sidecar": sidecar, "container": redroid}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    finally:
        if client:
            client.close()


def remove_vpc_sidecar(port: int) -> dict:
    """移除指定端口云机的 VPC sidecar。"""
    if not _ssh_enabled():
        return {"ok": False, "error": "SSH 未配置"}
    cfg = _ssh_config()
    sidecar = _vpc_sidecar_name(port, cfg["prefix"])
    client = None
    try:
        client = _ssh_connect()
        rc, out, err = _ssh_exec(client, f"docker rm -f {sidecar}", timeout=20)
        if rc != 0 and "No such container" not in (err or "") and "No such container" not in (out or ""):
            return {"ok": False, "error": (err or out or "删除 sidecar 失败").strip()[:240]}
        return {"ok": True, "sidecar": sidecar}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    finally:
        if client:
            client.close()


def probe_device_egress_ip(serial: str) -> str:
    """尽量读取云机当前出口 IP（失败返回空串）。"""
    check_url = "https://ifconfig.me/ip"
    try:
        check_url = current_app.config.get("VPC_EGRESS_CHECK_URL") or check_url
    except RuntimeError:
        check_url = os.environ.get("VPC_EGRESS_CHECK_URL", check_url)
    try:
        rc, out, _ = _run_adb(
            ["-s", serial, "shell", "toybox", "wget", "-qO-", check_url],
            timeout=18,
        )
        text = (out.decode("utf-8", errors="replace") if isinstance(out, bytes) else (out or "")).strip()
        ip = text.splitlines()[-1].strip() if text else ""
        if rc == 0 and ip and ip.count(".") == 3:
            return ip
    except Exception:  # noqa: BLE001
        pass
    return ""


def _restore_vpc_sidecar(port: int) -> None:
    """开机/重启后按客户网络绑定的出口线路重建 sidecar（失败不阻断主流程）。"""
    try:
        from .extensions import db
        from .models import Device, Vpc, VpcDevice, VpcLine
        d = db.session.query(Device).filter(
            Device.backend == "redroid",
            Device.serial.endswith(f":{int(port)}"),
        ).first()
        if d is None:
            return
        vd = db.session.query(VpcDevice).filter_by(device_id=d.id).first()
        if vd is None:
            return
        vpc = db.session.get(Vpc, vd.vpc_id)
        if vpc is None or not vpc.vpc_line_id:
            return
        line = db.session.get(VpcLine, vpc.vpc_line_id)
        if line is None or line.status != "running":
            return
        apply_vpc_sidecar(int(port), line.host, line.port, line.username or "", line.password or "")
    except Exception as e:  # noqa: BLE001
        try:
            current_app.logger.warning(f"恢复 VPC sidecar 失败 port={port}: {e}")
        except Exception:
            print(f"[vpc] restore sidecar failed port={port}: {e}")


def redroid_boot_args(port: int) -> str:
    """redroid 容器的 androidboot / ro.* 启动参数（镜像参数之后的那一段）。"""
    from flask import current_app
    _gpu = current_app.config.get("REDROID_GPU_MODE", "guest")
    _w = current_app.config.get("REDROID_WIDTH", "720")
    _h = current_app.config.get("REDROID_HEIGHT", "1280")
    _dpi = current_app.config.get("REDROID_DPI", "320")
    _fps = str(current_app.config.get("REDROID_FPS", "") or "")
    _nb = str(current_app.config.get("REDROID_NATIVE_BRIDGE", "") or "")
    _brand = str(current_app.config.get("REDROID_PRODUCT_BRAND", "") or "")
    _manu = str(current_app.config.get("REDROID_PRODUCT_MANUFACTURER", "") or "")
    _model = str(current_app.config.get("REDROID_PRODUCT_MODEL", "") or "")
    _pname = str(current_app.config.get("REDROID_PRODUCT_NAME", "") or "")
    _locale = str(current_app.config.get("REDROID_LOCALE", "") or "")
    _serial = f"SN{port}"
    return (
        f"androidboot.redroid_gpu_mode={_gpu} "
        f"androidboot.redroid_width={_w} "
        f"androidboot.redroid_height={_h} "
        f"androidboot.redroid_dpi={_dpi} "
        + (f"androidboot.redroid_fps={_fps} " if _fps else "")
        + (f"androidboot.redroid_root_props=ro.product.locale={_locale} " if _locale else "")
        + (f"ro.product.brand={_brand} " if _brand else "")
        + (f"ro.product.manufacturer={_manu} " if _manu else "")
        + (f"ro.product.model={_model} " if _model else "")
        + (f"ro.product.name={_pname} " if _pname else "")
        + f"ro.serialno={_serial} "
        + f"androidboot.serialno={_serial}"
        + (f" androidboot.native_bridge={_nb}" if _nb else "")
    )
