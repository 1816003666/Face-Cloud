# -*- coding: utf-8 -*-
"""云手机系统镜像管理。

镜像 = 云手机服务器上的一张 Docker 镜像。平台只存元数据，实体在服务器上。

    GET    /api/images                  列表（内置 + 自己的）+ 可部署的服务器
    POST   /api/images                  从设备创建镜像（docker commit）
    POST   /api/images/upload           上传镜像包（可选同时 docker load 到服务器）
    GET    /api/images/<id>/download    导出镜像包
    POST   /api/images/<id>/deploy      部署：新建云手机 / 重建目标设备
    DELETE /api/images/<id>             删除镜像（docker rmi + 删记录 + 删源文件）

权限：全部要求登录。内置镜像所有人可见，自建镜像仅创建者可见；
只能对自己名下的设备做 commit 与部署。
失败一律返回明确错误，不做任何假成功。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Blueprint, after_this_request, current_app, g, jsonify, request, send_file
from sqlalchemy import or_
from werkzeug.utils import secure_filename

from .. import image_service, image_tasks
from ..auth import login_required, require_role, require_perm
from ..extensions import db
from ..models import Device, Server, SystemImage, record_audit

images_bp = Blueprint("images", __name__)

ALLOWED_EXT = (".tar", ".tar.gz", ".tgz", ".img", ".zip")
MAX_NEW_DEVICES = 20


def _spawn(app, tid: str, fn) -> None:
    """把长任务丢到后台线程跑。

    任务里自己 push app context（线程里没有请求上下文，current_app / db.session
    都拿不到），跑完自己收尾并 remove session。_spawn 只是最后一道保险：
    万一 fn 抛了没被自己捕获，也要把任务标成失败，不能让前端一直转圈。
    """
    def _run():
        with app.app_context():
            try:
                fn()
            except Exception as e:             # noqa: BLE001
                try:
                    image_tasks.finish(tid, False, error=str(e)[:300])
                except Exception:              # noqa: BLE001
                    pass
            finally:
                try:
                    db.session.remove()
                except Exception:              # noqa: BLE001
                    pass

    threading.Thread(target=_run, name="imgtask-%s" % tid, daemon=True).start()


def _fmt_size(n) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "未知"
    if n >= 1 << 30:
        return "%.2f GB" % (n / float(1 << 30))
    if n >= 1 << 20:
        return "%.1f MB" % (n / float(1 << 20))
    if n >= 1 << 10:
        return "%.0f KB" % (n / float(1 << 10))
    return "%d B" % n


def _utcnow():
    return datetime.utcnow()


def _store_dir() -> str:
    d = current_app.config.get("IMAGE_STORE_DIR", "instance/images")
    if not os.path.isabs(d):
        d = os.path.join(current_app.root_path, "..", d)
    d = os.path.normpath(d)
    os.makedirs(d, exist_ok=True)
    return d


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:40] or "image")


def _server_by_host(host: str):
    return db.session.query(Server).filter(Server.host == (host or "")).first()


def _host_port(device):
    m = re.match(r"^(.+):(\d+)$", device.serial or "")
    if not m:
        raise image_service.ImageError("该设备的 serial 不是 host:port 形式，无法定位容器")
    return m.group(1), int(m.group(2))


def _own_device(device_id, user_id):
    if not device_id:
        raise image_service.ImageError("请选择来源云手机")
    d = db.session.get(Device, int(device_id))
    if d is None:
        raise image_service.ImageError("设备不存在")
    if d.created_by != user_id:
        raise image_service.ImageError("只能操作自己名下的云手机")
    return d


def _load_into_server(img, server) -> str:
    """确保镜像在目标服务器上可用；上传型镜像第一次部署时按需 docker load。

    返回可用的镜像引用 repo:tag。
    """
    if img.ref():
        return img.ref()
    if not (img.file_path and os.path.isfile(img.file_path)):
        raise image_service.ImageError("该镜像既没有镜像引用，也没有可用的源文件")
    client = image_service.connect(server)
    try:
        out = image_service.load_image_from_file(client, img.file_path)
    finally:
        try:
            client.close()
        except Exception:      # noqa: BLE001
            pass
    m = re.search(r"Loaded image[s]?:\s*(\S+)", out or "")
    if not m:
        raise image_service.ImageError("docker load 结束但没解析到镜像名，请检查镜像包")
    ref = m.group(1).strip()
    if ":" in ref:
        img.repo, img.tag = ref.rsplit(":", 1)
    else:
        img.repo, img.tag = ref, "latest"
    img.server_id = server.id
    img.server_host = server.host
    db.session.commit()
    return img.ref()


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------
def _ensure_image_on_host(img, server, tid=None) -> str:
    """确保镜像在目标宿主机上可用。

    - 上传型镜像（有源文件、没有 ref）：走原来的 docker load
    - 容器镜像（有 ref）：就在这台就直接用；不在就 docker save → 拉回平台 → 推到目标 → docker load
    返回可用的镜像引用 repo:tag。
    """
    if not img.ref():
        return _load_into_server(img, server)
    if (img.server_host or "") == server.host:
        return img.ref()

    src = _server_by_host(img.server_host) if img.server_host else None
    if src is None:
        raise image_service.ImageError(
            "镜像来源服务器 %s 未登记，无法把镜像搬过去" % (img.server_host or "(未知)"))

    tmp = os.path.join(_store_dir(), "transfer-%d-%s.tar" % (img.id, uuid.uuid4().hex[:6]))
    ref = img.ref()
    try:
        if tid:
            image_tasks.step(tid, stage="从 %s 导出镜像（docker save）" % src.host)
        c1 = None
        try:
            c1 = image_service.connect(src)
            image_service.save_image_to_file(c1, ref, tmp)
        finally:
            if c1 is not None:
                try:
                    c1.close()
                except Exception:          # noqa: BLE001
                    pass

        size = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
        if tid:
            image_tasks.step(tid, stage="传到 %s 并导入（docker load，%s）" % (server.host, _fmt_size(size)))
        c2 = None
        try:
            c2 = image_service.connect(server)
            image_service.load_image_from_file(c2, tmp)
        finally:
            if c2 is not None:
                try:
                    c2.close()
                except Exception:          # noqa: BLE001
                    pass
        # 记下镜像现在也存在这台宿主上（下次就不用再搬）
        record_audit(img.owner_id, "transfer_image", "system_image", img.id,
                     {"from": src.host, "to": server.host, "ref": ref, "bytes": size})
        return ref
    finally:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass


@images_bp.get("/images")
@login_required
def list_images():
    rows = (db.session.query(SystemImage)
            .filter(or_(SystemImage.builtin.is_(True),
                        SystemImage.owner_id == g.current_user.id))
            .order_by(SystemImage.builtin.desc(), SystemImage.id.desc())
            .all())
    servers = db.session.query(Server).order_by(Server.id).all()
    return jsonify(
        images=[x.to_dict() for x in rows],
        servers=[{"id": s.id, "name": s.name, "host": s.host, "status": s.status,
                  "device_count": s.device_count or 0,
                  "port_range": "%d-%d" % (s.adb_port_start or 5555, s.adb_port_end or 5699)}
                 for s in servers],
        replace_enabled=bool(current_app.config.get("IMAGE_DEPLOY_REPLACE_ENABLED", False)),
    )


# ---------------------------------------------------------------------------
# 从设备创建镜像
# ---------------------------------------------------------------------------
@images_bp.post("/images")
@require_perm("image.manage")
def create_image():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(error="请填写镜像名称"), 400
    version = (data.get("version") or "12.0").strip() or "12.0"
    desc = (data.get("desc") or "").strip()

    try:
        device = _own_device(data.get("device_id"), g.current_user.id)
        if (device.backend or "") != "redroid":
            raise image_service.ImageError("只有 redroid 云手机能制作镜像（当前 %s）" % device.backend)
        host, port = _host_port(device)
        server = _server_by_host(host)
        if server is None:
            raise image_service.ImageError("设备所在服务器 %s 未在平台登记，无法连接" % host)
    except image_service.ImageError as e:
        return jsonify(error=str(e)), 400

    img = SystemImage(name=name, version=version, desc=desc, status="creating",
                      server_id=server.id, server_host=server.host,
                      source_device_id=device.id, owner_id=g.current_user.id,
                      tag=version)
    db.session.add(img)
    db.session.commit()                    # 先落库拿 id，用它生成镜像名

    prefix = current_app.config.get("IMAGE_REPO_PREFIX", "cpimg")
    img.repo = "%s-%s-%d" % (prefix, _slug(name), img.id)
    db.session.commit()

    # 异步：docker commit 随容器里装了多少东西要几十秒到几分钟，
    # 同步跑的话客户端/nginx 任一环超时，用户就以为失败了（其实还在跑）。
    # 改成立刻返回 task_id，前端轮询 /images/tasks/<id> 看真实进度。
    total = 4                              # 连接 + 定位容器 + docker commit + 记录大小
    tid = image_tasks.create(
        "make_docker", "从设备创建镜像",
        "%s · %s（端口 %d）" % (name, server.host, port),
        owner_id=g.current_user.id, total=total)
    image_tasks.step(tid, stage="已提交", image_id=img.id)
    app = current_app._get_current_object()
    img_id = img.id
    _spawn(app, tid, lambda: _run_create_container(tid, img_id, server.id, port, server.host))
    return jsonify(task_id=tid, image_id=img.id, image=img.to_dict()), 202


def _run_create_container(tid, img_id, server_id, port, host):
    """后台线程里执行 docker commit（自带 app context）。"""
    img = db.session.get(SystemImage, img_id)
    if img is None:
        image_tasks.finish(tid, False, error="镜像记录已不存在")
        return
    server = db.session.get(Server, server_id)
    total = 4
    client = None
    try:
        image_tasks.step(tid, stage="连接服务器 %s" % host, done=1, total=total)
        client = image_service.connect(server)
        image_tasks.step(tid, stage="定位端口 %d 对应的容器" % port, done=2, total=total)
        container = image_service.container_for_port(client, port)
        if not container:
            raise image_service.ImageError("在 %s 上没找到端口 %d 对应的容器" % (host, port))
        # commit 期间没有任何字节级进度，起个心跳把"已用 N 秒"刷到阶段文字里，
        # 否则进度条会在 75% 停几十秒，看着像卡死了。
        _stop = threading.Event()
        _t0 = time.time()
        def _beat():
            while not _stop.wait(3):
                try:
                    image_tasks.step(tid, stage="docker commit（已用 %d 秒，容器越大越慢）" % int(time.time() - _t0))
                except Exception:
                    pass
        threading.Thread(target=_beat, name="imgbeat-%s" % tid, daemon=True).start()
        try:
            image_service.commit_container(client, container, img.ref())
        finally:
            _stop.set()
        image_tasks.step(tid, stage="记录镜像大小", done=3, total=total)
        img.size_bytes = image_service.image_size_bytes(client, img.ref())
        img.status = "ready"
        img.error = ""
        db.session.commit()
        record_audit(img.owner_id, "create_image", "system_image", img.id,
                     {"name": img.name, "ref": img.ref(), "async": True})
        image_tasks.finish(tid, True,
                           message="镜像「%s」已创建（%s）" % (img.name, _fmt_size(img.size_bytes)),
                           image_id=img.id)
    except Exception as e:                 # noqa: BLE001
        db.session.rollback()
        try:
            bad = db.session.get(SystemImage, img_id)
            if bad is not None:
                db.session.delete(bad)     # 失败不留幽灵记录
                db.session.commit()
        except Exception:                  # noqa: BLE001
            db.session.rollback()
        msg = str(e) if isinstance(e, image_service.ImageError) else ("创建镜像失败：%s" % e)
        image_tasks.finish(tid, False, error=msg)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:              # noqa: BLE001
                pass


@images_bp.post("/images/upload")
@require_perm("image.manage")
def upload_image():
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error="请选择镜像文件"), 400
    fname = secure_filename(f.filename) or "image.tar"
    if not fname.lower().endswith(ALLOWED_EXT):
        return jsonify(error="只支持 %s 格式" % " / ".join(ALLOWED_EXT)), 400

    name = (request.form.get("name") or "").strip() or os.path.splitext(fname)[0]
    version = (request.form.get("version") or "12.0").strip() or "12.0"
    desc = (request.form.get("desc") or "").strip()
    server_raw = (request.form.get("server_id") or "").strip()

    stored = os.path.join(_store_dir(), "%s-%s" % (uuid.uuid4().hex[:8], fname))
    f.save(stored)
    size = os.path.getsize(stored)
    sha = hashlib.sha256()
    with open(stored, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)

    server = None
    if server_raw:
        server = db.session.get(Server, int(server_raw)) if server_raw.isdigit() else None
        if server is None:
            os.remove(stored)
            return jsonify(error="所选服务器不存在"), 400

    img = SystemImage(name=name, version=version, desc=desc, status="ready",
                      file_path=stored, file_name=fname, size_bytes=size,
                      checksum=sha.hexdigest(), owner_id=g.current_user.id, tag=version)
    if server is not None:
        img.server_id = server.id
        img.server_host = server.host
    db.session.add(img)
    db.session.commit()

    # 指定了服务器就把镜像包直接 load 进去，省得部署时再传一次
    if server is not None:
        try:
            _load_into_server(img, server)
            img.error = ""
        except Exception as e:             # noqa: BLE001
            img.status = "error"
            img.error = "镜像包已保存到平台，但导入服务器失败：%s" % e
        db.session.commit()

    record_audit(g.current_user.id, "upload_image", "system_image", img.id,
                 {"name": name, "file": fname, "size": size})
    return jsonify(img.to_dict()), 201


# ---------------------------------------------------------------------------
# 导出下载
# ---------------------------------------------------------------------------
@images_bp.get("/images/<int:image_id>/download")
@login_required
def download_image(image_id):
    img = db.session.get(SystemImage, image_id)
    if img is None or not (img.builtin or img.owner_id == g.current_user.id):
        return jsonify(error="镜像不存在"), 404

    if img.file_path and os.path.isfile(img.file_path):
        return send_file(img.file_path, as_attachment=True,
                         download_name=img.file_name or ("%s.tar" % _slug(img.name)))

    if img.ref() and img.server_host:
        server = _server_by_host(img.server_host)
        if server is None:
            return jsonify(error="镜像所在服务器 %s 未登记" % img.server_host), 400
        tmp = os.path.join(_store_dir(), "export-%d-%s.tar" % (img.id, uuid.uuid4().hex[:6]))
        client = None
        try:
            client = image_service.connect(server)
            image_service.save_image_to_file(client, img.ref(), tmp)
        except Exception as e:             # noqa: BLE001
            if os.path.isfile(tmp):
                os.remove(tmp)
            return jsonify(error="从服务器导出镜像失败：%s" % e), 502
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:          # noqa: BLE001
                    pass
        # send_file 已经把文件打开（fd 已持有），这里删掉目录项是安全的；
        # 不删的话每下载一次容器镜像就在 instance/images 里漏一个几 GB 的 export-*.tar。
        @after_this_request
        def _drop_tmp(resp):
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return resp
        return send_file(tmp, as_attachment=True, download_name="%s.tar" % _slug(img.name))

    return jsonify(error="该镜像没有源文件，也无法从服务器导出"), 404


# ---------------------------------------------------------------------------
# 部署
# ---------------------------------------------------------------------------
@images_bp.post("/images/<int:image_id>/deploy")
@require_perm("image.manage")
def deploy_image(image_id):
    img = db.session.get(SystemImage, image_id)
    if img is None or not (img.builtin or img.owner_id == g.current_user.id):
        return jsonify(error="镜像不存在"), 404
    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "create").strip()
    if mode == "create":
        return _deploy_create(img, data)
    if mode == "replace":
        return _deploy_replace(img, data)
    return jsonify(error="未知的部署模式：%s" % mode), 400


def _deploy_create(img, data):
    """用镜像新建云手机（不动任何现有设备）。"""
    raw = data.get("count")
    try:
        count = 1 if raw in (None, "") else int(raw)
    except (TypeError, ValueError):
        return jsonify(error="数量必须是整数"), 400
    if count < 1 or count > MAX_NEW_DEVICES:
        return jsonify(error="一次可新建 1-%d 台" % MAX_NEW_DEVICES), 400

    sid = data.get("server_id")
    server = db.session.get(Server, int(sid)) if sid else None
    if server is None:
        return jsonify(error="请选择要部署到的服务器"), 400

    from .. import orchestrator
    created, errors = [], None
    client = None
    try:
        client = image_service.connect(server)
        ref = _load_into_server(img, server)
        ports = image_service.free_ports(client, server.adb_port_start or 5555,
                                         server.adb_port_end or 5699, count)
        for port in ports:
            name = "%s-%d" % (server.container_prefix or "redroid", port)
            image_service.run_redroid(client, name, port, ref)
            created.append(port)
    except Exception as e:                 # noqa: BLE001
        errors = str(e) if isinstance(e, image_service.ImageError) else ("部署失败：%s" % e)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:              # noqa: BLE001
                pass

    rows = []
    for port in created:
        dev = Device(name="%s-%d" % (img.name[:40], port),
                     serial="%s:%d" % (server.host, port), backend="redroid",
                     ip=server.host, status="running", created_by=g.current_user.id,
                     last_seen=_utcnow())
        db.session.add(dev)
        rows.append(dev)
    if rows:
        db.session.commit()
    for dev in rows:
        try:
            orchestrator.adb_connect(dev.serial)
        except Exception:                  # noqa: BLE001 - adb 连不上不影响登记
            pass

    if not created and errors:
        return jsonify(error=errors), 502
    record_audit(g.current_user.id, "deploy_image", "system_image", img.id,
                 {"mode": "create", "server": server.host, "ports": created})
    return jsonify(ok=True, mode="create", server=server.host,
                   devices=[{"id": d.id, "name": d.name, "serial": d.serial} for d in rows],
                   warning=errors), (200 if not errors else 207)


def _deploy_replace(img, data):
    """用镜像重建目标设备（会销毁原容器，可选清空数据卷）。"""
    if not current_app.config.get("IMAGE_DEPLOY_REPLACE_ENABLED", False):
        return jsonify(error="服务器未开启「重建目标设备」，请让管理员设置 "
                             "IMAGE_DEPLOY_REPLACE_ENABLED=true"), 501

    ids = data.get("device_ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify(error="请选择要重建的云手机"), 400
    wipe = bool(data.get("wipe_data"))

    done, failed = [], []
    for did in ids:
        try:
            dev = _own_device(did, g.current_user.id)
            host, port = _host_port(dev)
            server = _server_by_host(host)
            if server is None:
                raise image_service.ImageError("服务器 %s 未登记" % host)
            client = image_service.connect(server)
            try:
                ref = _ensure_image_on_host(img, server)
                name = image_service.container_for_port(client, port)
                if name:
                    image_service.remove_container(client, name)
                if wipe:
                    image_service.wipe_data_dir(client, port)
                image_service.run_redroid(
                    client, name or "%s-%d" % (server.container_prefix or "redroid", port),
                    port, ref)
            finally:
                try:
                    client.close()
                except Exception:          # noqa: BLE001
                    pass
            done.append({"device_id": dev.id, "name": dev.name, "serial": dev.serial})
        except Exception as e:             # noqa: BLE001
            failed.append({"device_id": did,
                           "error": str(e) if isinstance(e, image_service.ImageError) else str(e)})

    record_audit(g.current_user.id, "deploy_image", "system_image", img.id,
                 {"mode": "replace", "wipe_data": wipe,
                  "done": [d["device_id"] for d in done],
                  "failed": [f["device_id"] for f in failed]})
    return jsonify(ok=not failed, mode="replace", wipe_data=wipe,
                   replaced=done, failed=failed)


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------
@images_bp.delete("/images/<int:image_id>")
@require_perm("image.manage")
def delete_image(image_id):
    img = db.session.get(SystemImage, image_id)
    if img is None or not (img.builtin or img.owner_id == g.current_user.id):
        return jsonify(error="镜像不存在"), 404
    if img.builtin:
        return jsonify(error="内置镜像不能删除"), 400

    warnings = []
    if img.ref() and img.server_host:
        server = _server_by_host(img.server_host)
        if server is None:
            warnings.append("服务器 %s 未登记，未在服务器上删除" % img.server_host)
        else:
            client = None
            try:
                client = image_service.connect(server)
                image_service.remove_image(client, img.ref())
            except Exception as e:         # noqa: BLE001
                warnings.append("服务器上删除镜像失败（记录仍会删除）：%s" % e)
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:      # noqa: BLE001
                        pass

    if img.file_path and os.path.isfile(img.file_path):
        try:
            os.remove(img.file_path)
        except OSError as e:
            warnings.append("删除源文件失败：%s" % e)

    db.session.delete(img)
    db.session.commit()
    record_audit(g.current_user.id, "delete_image", "system_image", image_id,
                 {"name": img.name, "warnings": warnings})
    return jsonify(ok=True, warnings=warnings)

# ---------------------------------------------------------------------------
# 真机（板子）分区镜像
# ---------------------------------------------------------------------------
# 板子 SSH 地址来自配置（IMAGE_BOARD_HOSTS），统一用一把 root 私钥登录。
# 不复用 Device 记录：平台里真机的 relay 记录（127.0.0.1:36xxx）不带板子网络地址。
_board_cache = {"at": 0.0, "rows": [], "devmap_at": 0.0, "devmap": {}}


def _hw_serial_of(serial: str) -> str:
    """用平台自己的 adb 读设备的硬件序列号（读不到返回空）。

    真机的 platform serial 是 relay 地址（127.0.0.1:36xxx），看不出是哪块板子；
    只有 adb getprop ro.serialno 才能和板子的 ro.serialno 对上。
    """
    try:
        from .. import orchestrator
        rc, out, err = orchestrator._adb_shell(serial, "getprop ro.serialno", timeout=10)
        if isinstance(out, bytes):
            return out.decode("utf-8", "replace").strip()
        return str(out or "").strip()
    except Exception:                          # noqa: BLE001
        return ""


def _device_hw_map(force: bool = False) -> dict:
    """硬件序列号 -> 平台设备（含所属服务器）。带 5 分钟缓存。"""
    now = time.time()
    if not force and (now - _board_cache["devmap_at"]) < 300 and _board_cache["devmap"]:
        return _board_cache["devmap"]
    mapping = {}
    try:
        rows = (db.session.query(Device)
                .filter(or_(Device.serial.like("127.0.0.1:36%"),
                            Device.serial.like("192.168.99.%")))
                .all())
        server_names = {s.host: s.name for s in db.session.query(Server).all()}
        for dev in rows:
            hw = _hw_serial_of(dev.serial)
            if hw:
                mapping[hw] = {
                    "device_id": dev.id,
                    "device_name": dev.name,
                    "device_serial": dev.serial,
                    "server": server_names.get(dev.ip, dev.ip or ""),
                }
    except Exception:                          # noqa: BLE001
        mapping = {}
    _board_cache["devmap"] = mapping
    _board_cache["devmap_at"] = now
    return mapping


def _board_cfg():
    cfg = current_app.config
    hosts = [h.strip() for h in str(cfg.get("IMAGE_BOARD_HOSTS", "") or "").split(",") if h.strip()]
    return {
        "hosts": hosts,
        "user": cfg.get("IMAGE_BOARD_USER", "root"),
        "key": cfg.get("IMAGE_BOARD_KEY", ""),
        "port": int(cfg.get("IMAGE_BOARD_PORT", 22)),
    }


def _board_connect(host: str, cfg=None):
    # cfg 一定要能在**线程池外**取好再传进来：current_app 是线程局部的，
    # 在工作线程里读它会直接 RuntimeError（踩过一次，10 块板子全变离线）。
    cfg = cfg or _board_cfg()
    if not cfg["key"]:
        raise image_service.ImageError("未配置板子私钥（IMAGE_BOARD_KEY），无法连接真机")
    if not os.path.isfile(cfg["key"]):
        raise image_service.ImageError("板子私钥不存在：%s" % cfg["key"])
    return image_service.board_connect(
        host, port=cfg["port"], username=cfg["user"], secret=cfg["key"], auth_type="key")


def _scan_one_board(host: str, cfg: dict, devmap: dict) -> dict:
    """扫一块板子。跑在线程池里，**既不碰数据库、也不读 current_app**。

    SQLAlchemy 的 session 和 Flask 的 current_app 都是线程局部的，
    两者都必须在进线程池之前取好传进来。
    """
    row = {"host": host, "online": False, "error": "", "serial": "", "model": "",
           "android": "", "partitions": [], "device_id": None, "device_name": "",
           "device_serial": "", "server": ""}
    client = None
    try:
        client = _board_connect(host, cfg)
        rc, out, err = image_service._sh(
            client,
            "echo \"serial=$(getprop ro.serialno)\"; echo \"model=$(getprop ro.product.model)\"; "
            "echo \"android=$(getprop ro.build.version.release)\"", timeout=30)
        for line in (out or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip() in row:
                    row[k.strip()] = v.strip()
        row["partitions"] = image_service.board_partition_info(client)
        row["online"] = True
        m = (row.get("serial") and devmap.get(row["serial"])) or None
        if m:
            row.update(m)
    except Exception as e:                     # noqa: BLE001
        row["error"] = str(e)[:160]
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:                  # noqa: BLE001
                pass
    return row


def _scan_boards(force: bool = False) -> list:
    """扫描配置的板子，返回 [{host, online, serial, model, android, partitions}]。带 60s 缓存。

    10 块板子**串行** SSH 要 17 秒以上，会顶掉客户端 15s 超时 → 改成并发扫。
    板子→平台设备的映射（要查库）必须在进线程池之前算好。
    """
    cfg = _board_cfg()
    if not cfg["hosts"]:
        return []
    if not force and (time.time() - _board_cache["at"]) < 60 and _board_cache["rows"]:
        return _board_cache["rows"]

    devmap = _device_hw_map()          # 查库：必须在主线程
    workers = max(1, min(10, len(cfg["hosts"])))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(lambda h: _scan_one_board(h, cfg, devmap), cfg["hosts"]))

    _board_cache["at"] = time.time()
    _board_cache["rows"] = rows
    return rows


@images_bp.get("/images/boards")
@login_required
def list_boards():
    """真机板子清单 + 每个分区的状态（据此判断能不能在线刷）。"""
    return jsonify(
        boards=_scan_boards(force=request.args.get("fresh") == "1"),
        partitions=[{"name": k, **v} for k, v in image_service.DEVICE_PARTITIONS.items()],
    )


@images_bp.post("/images/device")
@require_perm("image.manage")
def create_device_image():
    """从真机板子创建分区镜像：dd 各分区 → 打包 → 拉到平台。只读操作。"""
    data = request.get_json(silent=True) or {}
    host = (data.get("board_host") or "").strip()
    cfg = _board_cfg()
    if host not in cfg["hosts"]:
        return jsonify(error="板子 %s 不在配置清单里" % (host or "(空)")), 400
    parts = data.get("partitions") or []
    if not isinstance(parts, list) or not parts:
        return jsonify(error="请至少选择一个分区"), 400
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(error="请填写镜像名称"), 400

    img = SystemImage(name=name, version=(data.get("version") or "10").strip() or "10",
                      desc=(data.get("desc") or "").strip(), kind="device",
                      status="creating", server_host=host,
                      partitions=json.dumps(parts, ensure_ascii=False),
                      owner_id=g.current_user.id, tag="")
    db.session.add(img)
    db.session.commit()                        # 先拿 id 生成文件名

    # 异步：立刻返回 task_id，导出在后台线程跑，前端轮询 /images/tasks/<id> 看真实进度。
    # 同步跑一整趟的话，nginx/客户端任一环超时用户就以为失败了（实际还在跑）。
    total = len(parts) + 2                     # N 次 dd + 打包 + 拉回
    tid = image_tasks.create("make_device", "从板子创建镜像",
                             "%s · %s（%s）" % (name, host, "/".join(parts)),
                             owner_id=g.current_user.id, total=total)
    image_tasks.step(tid, stage="连接板子", image_id=img.id)
    app = current_app._get_current_object()
    img_id, uid = img.id, g.current_user.id
    _spawn(app, tid, lambda: _run_make_device(tid, img_id, uid, host, parts, name, total))
    return jsonify(task_id=tid, image_id=img.id, image=img.to_dict()), 202


def _run_make_device(tid, img_id, uid, host, parts, name, total):
    """后台线程里执行真机镜像导出（自带 app context）。"""
    img = db.session.get(SystemImage, img_id)
    if img is None:
        image_tasks.finish(tid, False, error="镜像记录已不存在")
        return
    client = None
    remote = None

    def _prog(done, _tot, stage):
        image_tasks.step(tid, stage=stage, done=done, total=total)

    try:
        client = _board_connect(host)
        rc, out, _ = image_service._sh(client, "getprop ro.serialno", timeout=30)
        img.source_serial = (out or "").strip()
        remote, _files = image_service.dump_partitions(
            client, parts, tag="cpimg-%d" % img.id, on_progress=_prog, total_steps=total)
        board_size = image_service.board_file_size(client, remote)
        stored = os.path.join(_store_dir(), "dev-%d-%s.tar.gz" % (img.id, _slug(name)))

        image_tasks.step(tid, stage="传到平台（sftp）", done=len(parts) + 1, total=total)

        def _bytes(n, tot_bytes):
            if tot_bytes:
                image_tasks.step(tid, stage="传到平台（%d%%）" % min(100, int(n * 100 / tot_bytes)))

        image_service.pull_board_file(client, remote, stored, on_bytes=_bytes)
        image_service.cleanup_board(client, remote)   # 板子上的包只是中转
        remote = None

        image_tasks.step(tid, stage="校验（sha256）", done=total, total=total)
        img.file_path = stored
        img.file_name = os.path.basename(stored)
        img.size_bytes = os.path.getsize(stored) if os.path.isfile(stored) else board_size
        sha = hashlib.sha256()
        with open(stored, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                sha.update(chunk)
        img.checksum = sha.hexdigest()
        img.status = "ready"
        img.error = ""
        db.session.commit()
        size_txt = _fmt_size(img.size_bytes)
        record_audit(uid, "create_device_image", "system_image", img.id,
                     {"board": host, "partitions": parts})
        image_tasks.finish(tid, True, message="镜像「%s」已创建（%s）" % (img.name, size_txt))
    except Exception as e:                     # noqa: BLE001
        if remote and client is not None:
            image_service.cleanup_board(client, remote)
        try:
            db.session.rollback()
        except Exception:                      # noqa: BLE001
            pass
        try:
            ghost = db.session.get(SystemImage, img_id)
            if ghost is not None:
                db.session.delete(ghost)       # 失败不留幽灵记录
                db.session.commit()
        except Exception:                      # noqa: BLE001
            pass
        msg = str(e) if isinstance(e, image_service.ImageError) else ("制作镜像失败：%s" % e)
        image_tasks.finish(tid, False, error=msg)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:                  # noqa: BLE001
                pass


@images_bp.post("/images/<int:image_id>/deploy-device")
@require_perm("image.manage")
def deploy_device_image(image_id):
    """把真机分区镜像写回目标板子。

    运行中被占用的分区（super）由 image_service 拒绝，不会把设备写坏。
    """
    img = db.session.get(SystemImage, image_id)
    if img is None or not (img.builtin or img.owner_id == g.current_user.id):
        return jsonify(error="镜像不存在"), 404
    if (img.kind or "docker") != "device":
        return jsonify(error="这不是真机分区镜像"), 400
    if not (img.file_path and os.path.isfile(img.file_path)):
        return jsonify(error="镜像源文件已丢失，无法部署"), 400

    data = request.get_json(silent=True) or {}
    targets = data.get("board_hosts") or []
    parts = data.get("partitions") or img.partition_list
    cfg = _board_cfg()
    if not isinstance(targets, list) or not targets:
        return jsonify(error="请选择目标板子"), 400
    bad = [t for t in targets if t not in cfg["hosts"]]
    if bad:
        return jsonify(error="板子不在配置清单里：%s" % "、".join(bad)), 400

    # 异步：和创建镜像一样，返回 task_id 由前端轮询真实进度
    per_board = len(parts) + 2                 # 上传 + 解包 + 逐分区写回
    tid = image_tasks.create("deploy_device", "刷入真机镜像",
                             "%s → %s（%s）" % (img.name, "、".join(targets), "/".join(parts)),
                             owner_id=g.current_user.id, total=len(targets) * per_board)
    image_tasks.step(tid, stage="准备中", image_id=img.id)
    app = current_app._get_current_object()
    img_id, uid = img.id, g.current_user.id
    _spawn(app, tid, lambda: _run_deploy_device(tid, img_id, uid, list(targets), list(parts)))
    return jsonify(task_id=tid, image_id=img.id), 202


def _run_deploy_device(tid, image_id, uid, targets, parts):
    """后台线程里把镜像写回板子（自带 app context）。"""
    img = db.session.get(SystemImage, image_id)
    if img is None or not os.path.isfile(img.file_path or ""):
        image_tasks.finish(tid, False, error="镜像不存在或源文件已丢失")
        return

    per_board = len(parts) + 2
    total = len(targets) * per_board
    done, failed = [], []
    for bi, host in enumerate(targets):
        base = bi * per_board
        client = None
        try:
            image_tasks.step(tid, stage="连接 %s（%d/%d）" % (host, bi + 1, len(targets)),
                             done=base, total=total)
            client = _board_connect(host)
            image_service.restore_partitions(
                client, img.file_path, parts,
                on_progress=lambda d, t, s: image_tasks.step(tid, stage=s, done=d, total=t),
                base=base, total_steps=total)
            done.append(host)
        except Exception as e:                 # noqa: BLE001
            failed.append({"host": host,
                           "error": str(e) if isinstance(e, image_service.ImageError) else str(e)})
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:              # noqa: BLE001
                    pass
        image_tasks.step(tid, done=base + per_board, total=total)

    try:
        record_audit(uid, "deploy_device_image", "system_image", image_id,
                     {"partitions": parts, "done": done, "failed": [f["host"] for f in failed]})
    except Exception:                          # noqa: BLE001
        pass
    if failed:
        image_tasks.finish(tid, False,
                           error="已刷 %d 台，失败 %d 台：%s"
                                 % (len(done), len(failed),
                                    "；".join("%s %s" % (f["host"], f["error"]) for f in failed)))
    else:
        image_tasks.finish(tid, True, message="已刷入 %d 台板子（分区：%s）"
                                              % (len(done), "/".join(parts)))


# ---------------------------------------------------------------------------
# 长任务查询（前端轮询进度）
# ---------------------------------------------------------------------------
@images_bp.get("/images/tasks")
@login_required
def list_image_tasks():
    """我发起的镜像任务（新的在前，含刚跑完的）。客户端重开也能靠它恢复进度显示。"""
    return jsonify(image_tasks.list_for(g.current_user.id))


@images_bp.get("/images/tasks/<tid>")
@login_required
def get_image_task(tid):
    t = image_tasks.get(tid, g.current_user.id)
    if t is None:
        return jsonify(error="任务不存在或已过期"), 404
    return jsonify(t)

