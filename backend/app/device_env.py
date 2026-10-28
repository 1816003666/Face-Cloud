# -*- coding: utf-8 -*-
"""云手机环境伪装：给每台 redroid 容器一套独立、像真机的设备指纹。

背景：redroid 默认 `ro.product.model=redroidXX_arm64`、0 个传感器、无 IMEI、
userdebug 系统，微信 / 抖音这类风控严格的 App 会直接判定"异常环境"，
表现为安全限制、限制登录、环境异常。

做法：
- 建容器时用 `ro.product.*` / `ro.build.fingerprint` / `ro.serialno` 启动参数
  覆盖机型信息（已实测 redroid 11 镜像支持，getprop 能看到覆盖结果）；
- 指纹从真机模板池里取，同一时间全平台不重复；
- 时区 / 语言在容器起来后用 adb setprop + settings 补上。
"""
from __future__ import annotations

import hashlib
import json
import random

# 真机模板池：brand / manufacturer / model / device / name / android / fingerprint
TEMPLATES = [
    # model 用真机 ro.product.model 的值（型号代码，不带空格：redroid 解析属性时按空格切分）
    {"brand": "Redmi", "manufacturer": "Xiaomi", "model": "2201117TG",
     "model_name": "Redmi Note 11", "device": "spes", "name": "spes_global", "android": "11",
     "fingerprint": "Redmi/spes_global/spes:11/RKQ1.211103.001/V13.0.8.0.RGCMIXM:user/release-keys"},
    {"brand": "Redmi", "manufacturer": "Xiaomi", "model": "M2012K11AC",
     "model_name": "Redmi K40", "device": "alioth", "name": "alioth", "android": "12",
     "fingerprint": "Redmi/alioth/alioth:12/SKQ1.211103.001/V13.0.7.0.SKHCNXM:user/release-keys"},
    {"brand": "Xiaomi", "manufacturer": "Xiaomi", "model": "2201123C",
     "model_name": "Xiaomi 12", "device": "cupid", "name": "cupid", "android": "12",
     "fingerprint": "Xiaomi/cupid/cupid:12/SKQ1.220119.001/V13.0.24.0.SLCCNXM:user/release-keys"},
    {"brand": "OPPO", "manufacturer": "OPPO", "model": "PEPM00",
     "model_name": "OPPO Reno6 5G", "device": "OP4A7B", "name": "OP4A7B", "android": "11",
     "fingerprint": "OPPO/OP4A7B/OP4A7B:11/RKQ1.210607.001/1638449000000:user/release-keys"},
    {"brand": "vivo", "manufacturer": "vivo", "model": "V2109A",
     "model_name": "vivo Y33s", "device": "V2109", "name": "V2109", "android": "11",
     "fingerprint": "vivo/V2109/V2109:11/RP1A.200720.012/compiler220120133655:user/release-keys"},
    {"brand": "HONOR", "manufacturer": "HONOR", "model": "ANY-AN00",
     "model_name": "HONOR X30", "device": "HONORANY", "name": "HONORANY-AN00", "android": "11",
     "fingerprint": "HONOR/HONORANY/HONORANY:11/HONORANY-AN00/4.2.0.196:user/release-keys"},
    {"brand": "samsung", "manufacturer": "samsung", "model": "SM-A5260",
     "model_name": "Galaxy A52 5G", "device": "a52xq", "name": "a52xqchn", "android": "12",
     "fingerprint": "samsung/a52xqchn/a52xq:12/SP1A.210812.016/A5260ZHU3CVB2:user/release-keys"},
    {"brand": "OnePlus", "manufacturer": "OnePlus", "model": "LE2100",
     "model_name": "OnePlus 9R", "device": "OnePlus9R", "name": "OnePlus9R", "android": "12",
     "fingerprint": "OnePlus/OnePlus9R/OnePlus9R:12/RKQ1.211119.001/1652700000000:user/release-keys"},
]

TIMEZONE = "Asia/Shanghai"
LOCALE = "zh-CN"


def generate_profile(port: int | None = None, used: set[str] | None = None,
                     salt: str = "") -> dict:
    """生成一套设备指纹。

    - 只给 port：同一端口稳定得到同一套（重装/重启不换机）；
    - 带 salt：换一套新的（用于"给已有设备换指纹"）。
    """
    used = used or set()
    seed = f"dev-{port}" if port is not None else str(random.random())
    if salt:
        seed = f"{seed}-{salt}"
    idx = int(hashlib.md5(seed.encode()).hexdigest(), 16) % len(TEMPLATES)
    tpl = TEMPLATES[idx]
    # 序列号：模板品牌前缀 + 端口（或随机），保证唯一
    if port and not salt:
        serial = f"{tpl['brand'][:2].upper()}{port:06d}"
    else:
        serial = f"{tpl['brand'][:2].upper()}{random.randint(100000, 999999)}"
    profile = {
        "brand": tpl["brand"],
        "manufacturer": tpl["manufacturer"],
        "model": tpl["model"],
        "model_name": tpl.get("model_name", tpl["model"]),
        "device": tpl["device"],
        "name": tpl["name"],
        "android_version": tpl["android"],
        "fingerprint": tpl["fingerprint"],
        "serial": serial,
        "timezone": TIMEZONE,
        "locale": LOCALE,
    }
    # 同机型重复时换下一个模板，直到不冲突
    for _ in range(len(TEMPLATES)):
        if profile["model"] not in used:
            break
        idx = (idx + 1) % len(TEMPLATES)
        tpl = TEMPLATES[idx]
        profile.update({k: tpl[k] for k in ("brand", "manufacturer", "model", "device",
                                           "name", "fingerprint", "android_version")})
        profile["model_name"] = tpl.get("model_name", tpl["model"])
    return profile


def docker_args(profile: dict) -> str:
    """生成 docker run 里的属性参数（redroid 把裸 key=value 当系统属性注入）。

    值里含空格（如 "Redmi Note 11"）必须整体加引号，否则会被 shell 拆成两个参数。
    """
    if not profile:
        return ""
    pairs = [
        ("ro.product.brand", profile.get("brand")),
        ("ro.product.manufacturer", profile.get("manufacturer")),
        ("ro.product.model", profile.get("model")),
        ("ro.product.device", profile.get("device")),
        ("ro.product.name", profile.get("name")),
        ("ro.build.fingerprint", profile.get("fingerprint")),
        ("ro.serialno", profile.get("serial")),
        ("androidboot.serialno", profile.get("serial")),
        ("ro.debuggable", "0"),
    ]
    out = []
    for key, val in pairs:
        if not val:
            continue
        arg = f"{key}={val}"
        if " " in str(val):
            arg = f"'{arg}'"
        out.append(arg)
    return " ".join(out)


def load(device) -> dict:
    """从 Device.fingerprint 里解析出指纹 JSON（兼容老的简单格式）。"""
    try:
        data = json.loads(getattr(device, "fingerprint", "") or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def dump(profile: dict) -> str:
    return json.dumps(profile, ensure_ascii=False)


def runtime_setup(serial: str, profile: dict) -> dict:
    """容器起来后补时区 / 语言（persist 属性不能靠启动参数注入）。"""
    from . import orchestrator
    tz = (profile or {}).get("timezone") or TIMEZONE
    locale = (profile or {}).get("locale") or LOCALE
    lang, _, region = locale.partition("-")
    cmds = [
        f"setprop persist.sys.timezone {tz}",
        f"setprop persist.sys.locale {locale}",
        f"setprop persist.sys.language {lang}",
        f"setprop persist.sys.country {region or 'CN'}",
        f"settings put system time_12_24 24",
        f"settings put system font_scale 1.0",
    ]
    results = {}
    for cmd in cmds:
        try:
            rc, out, err = orchestrator._adb_shell(serial, cmd, timeout=10)
            results[cmd.split()[0]] = "ok" if rc == 0 else "fail"
        except Exception as e:  # noqa: BLE001
            results[cmd.split()[0]] = f"err: {e}"
    return results


def sensor_status(serial: str) -> dict:
    """查设备上有几个传感器、sensor HAL 是否在跑。

    redroid 官方 arm64 镜像自带 mock HAL（10 个传感器）；有些自行编译的
    x86_64 镜像没有这个 HAL，dumpsys 会返回 "No Sensors on the device"。
    """
    from . import orchestrator
    out = ""
    try:
        rc, o, e = orchestrator._adb_shell(serial, "dumpsys sensorservice", timeout=15)
        out = (o or b"").decode("utf-8", "replace") + (e or b"").decode("utf-8", "replace")
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "count": 0, "hal": False, "error": str(ex)}
    count = out.count("type: android.sensor")
    hal = "No Sensors" not in out and count > 0
    return {"ok": True, "count": count, "hal": hal,
            "detail": "No Sensors on the device" if not hal else "ok"}
