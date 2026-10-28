"""文件管理模块：上传 / 下载 / 浏览 / 删除 / 推送到设备。

权限：上传/删除/推送需要 operator 及以上；浏览登录即可。
推送到设备：因当前为 simulator 模式，实际只做“记录日志 + 审计”，不真正写设备。
"""
import os
import uuid

from flask import request, jsonify, Blueprint, current_app, send_file, g
from werkzeug.utils import secure_filename

files_bp = Blueprint("files", __name__)

from .. import datascope
from ..extensions import db
from ..models import FileRecord, Device, DeviceLog, record_audit, iso_utc
from ..auth import login_required, require_role, require_perm
from ..orchestrator import push_file_to_device, install_apk_to_device, list_device_dir, read_device_file, pull_file_from_device, _adb_ascii_dir


def _upload_dir():
    """上传目录，统一解析为绝对路径（相对路径基于 backend/ 项目根）。"""
    d = current_app.config["UPLOAD_FOLDER"]
    if not os.path.isabs(d):
        # __file__ = backend/app/routes/files.py → 上两级 = backend/
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        d = os.path.join(base, d)
    os.makedirs(d, exist_ok=True)
    return d


@files_bp.post("/files/upload")
@require_perm("file.upload")
def upload():
    if "file" not in request.files:
        return jsonify(error="缺少文件字段 file"), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify(error="文件名不能为空"), 400
    original = f.filename
    # 磁盘名用 uuid 避免重名/中文路径问题，原文件名单独保留展示
    ext = os.path.splitext(original)[1]
    stored = uuid.uuid4().hex + ext
    f.save(os.path.join(_upload_dir(), stored))

    rec = FileRecord(
        filename=original,
        stored_name=stored,
        size=os.path.getsize(os.path.join(_upload_dir(), stored)),
        mime=f.mimetype or "",
        uploader_id=g.current_user.id,
    )
    db.session.add(rec)
    db.session.commit()
    record_audit(g.current_user.id, "upload_file", "file", rec.id,
                {"filename": original, "size": rec.size})
    return jsonify(_serialize(rec)), 201


def _file_type(name: str) -> str:
    """按扩展名判断文件类型：image / apk / doc / other。"""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico"):
        return "image"
    if ext == "apk":
        return "apk"
    if ext in ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "csv"):
        return "doc"
    return "other"


@files_bp.get("/files/stats")
@require_perm("file.view")
def file_stats():
    """文件存储概览：总数、总大小、按类型（图片/APK/文档/其他）统计。

    按角色的数据范围过滤（"仅我名下"只统计自己上传的）。
    """
    rows = datascope.apply_owner_scope(
        db.session.query(FileRecord), FileRecord.uploader_id, g.current_user).all()
    total = len(rows)
    total_size = sum(r.size for r in rows)
    by_type = {}
    for r in rows:
        t = _file_type(r.filename)
        b = by_type.setdefault(t, {"count": 0, "size": 0})
        b["count"] += 1
        b["size"] += r.size
    return jsonify(total_files=total, total_size=total_size, by_type=by_type)


@files_bp.get("/files")
@require_perm("file.view")
def list_files():
    page = int(request.args.get("page", 1))
    page_size = min(int(request.args.get("page_size", 20)), 100)
    q = (request.args.get("q") or "").strip()
    device_id = request.args.get("device_id")

    query = datascope.apply_owner_scope(
        db.session.query(FileRecord), FileRecord.uploader_id, g.current_user)
    if q:
        query = query.filter(FileRecord.filename.ilike(f"%{q}%"))
    if device_id:
        query = query.filter(FileRecord.target_device_id == int(device_id))
    total = query.count()
    rows = query.order_by(FileRecord.id.desc()) \
                .offset((page - 1) * page_size).limit(page_size).all()
    return jsonify({
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_serialize(r) for r in rows],
    })


@files_bp.get("/files/<int:fid>/download")
@require_perm("file.view")
def download(fid):
    rec = db.session.get(FileRecord, fid)
    if rec is None or not datascope.can_access_owner(rec.uploader_id, g.current_user):
        return jsonify(error="文件不存在"), 404
    return send_file(os.path.join(_upload_dir(), rec.stored_name),
                     as_attachment=True, download_name=rec.filename)


@files_bp.delete("/files/<int:fid>")
@require_perm("file.delete")
def delete_file(fid):
    rec = db.session.get(FileRecord, fid)
    if rec is None or not datascope.can_access_owner(rec.uploader_id, g.current_user):
        return jsonify(error="文件不存在"), 404
    try:
        os.remove(os.path.join(_upload_dir(), rec.stored_name))
    except OSError:
        pass
    db.session.delete(rec)
    db.session.commit()
    record_audit(g.current_user.id, "delete_file", "file", fid,
                {"filename": rec.filename})
    return jsonify(ok=True)


@files_bp.post("/files/<int:fid>/push")
@require_perm("file.push")
def push_file(fid):
    """把文件真实推送到指定设备的 /sdcard/Download/（adb push），逐台执行并回传结果。"""
    rec = db.session.get(FileRecord, fid)
    if rec is None:
        return jsonify(error="文件不存在"), 404
    device_ids = request.get_json(silent=True) or {}
    ids = device_ids.get("device_ids", [])
    if not ids:
        return jsonify(error="请选择至少一台设备"), 400
    from .. import scoping
    _bad = scoping.foreign_device_ids(g.current_user, ids)
    if _bad:
        return jsonify(error="无权操作部分设备", invalid_ids=_bad), 403

    local_path = os.path.join(_upload_dir(), rec.stored_name)
    if not os.path.isfile(local_path):
        return jsonify(error="文件在服务器上不存在，无法推送"), 404

    ok = 0
    results = []
    for did in ids:
        dev = db.session.get(Device, int(did))
        if dev is None:
            results.append({"device_id": int(did), "ok": False, "message": "设备不存在"})
            continue
        serial = dev.serial
        remote = f"/sdcard/Download/{rec.filename}"
        try:
            res = push_file_to_device(serial, local_path, remote)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "output": f"异常: {e}"}
        if res.get("ok"):
            ok += 1
        message = f"推送文件 {rec.filename} 到 {serial}" + (" 成功" if res.get("ok") else f" 失败: {res.get('output', '')[:120]}")
        db.session.add(DeviceLog(
            device_id=dev.id, level="info" if res.get("ok") else "error",
            message=message))
        results.append({
            "device_id": dev.id, "device_name": dev.name, "serial": serial,
            "ok": bool(res.get("ok")), "message": message,
            "remote_path": res.get("remote_path"),
        })
    rec.target_device_id = int(ids[0]) if ids else None
    db.session.commit()
    record_audit(g.current_user.id, "push_file", "file", fid,
                {"filename": rec.filename, "devices": ids, "ok": ok})
    return jsonify(ok=ok, failed=len(ids) - ok, results=results)


@files_bp.post("/files/<int:fid>/install")
@require_perm("file.push")
def install_file(fid):
    """把 APK 文件真实安装到指定设备（adb install -r），逐台执行并回传结果。"""
    rec = db.session.get(FileRecord, fid)
    if rec is None:
        return jsonify(error="文件不存在"), 404
    if not rec.filename.lower().endswith(".apk"):
        return jsonify(error="仅支持安装 APK 文件"), 400
    device_ids = request.get_json(silent=True) or {}
    ids = device_ids.get("device_ids", [])
    if not ids:
        return jsonify(error="请选择至少一台设备"), 400
    from .. import scoping
    _bad = scoping.foreign_device_ids(g.current_user, ids)
    if _bad:
        return jsonify(error="无权操作部分设备", invalid_ids=_bad), 403

    local_path = os.path.join(_upload_dir(), rec.stored_name)
    if not os.path.isfile(local_path):
        return jsonify(error="文件在服务器上不存在，无法安装"), 404

    ok = 0
    results = []
    for did in ids:
        dev = db.session.get(Device, int(did))
        if dev is None:
            results.append({"device_id": int(did), "ok": False, "message": "设备不存在"})
            continue
        serial = dev.serial
        try:
            res = install_apk_to_device(serial, local_path)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "output": f"异常: {e}"}
        if res.get("ok"):
            ok += 1
        message = f"安装 APK {rec.filename} 到 {serial}" + (" 成功" if res.get("ok") else f" 失败: {res.get('output', '')[:200]}")
        db.session.add(DeviceLog(
            device_id=dev.id, level="info" if res.get("ok") else "error",
            message=message))
        results.append({
            "device_id": dev.id, "device_name": dev.name, "serial": serial,
            "ok": bool(res.get("ok")), "message": message,
        })
    db.session.commit()
    record_audit(g.current_user.id, "install_apk", "file", fid,
                {"filename": rec.filename, "devices": ids, "ok": ok})
    return jsonify(ok=ok, failed=len(ids) - ok, results=results)


def _serialize(r: FileRecord):
    return {
        "id": r.id,
        "filename": r.filename,
        "size": r.size,
        "mime": r.mime,
        "uploader_id": r.uploader_id,
        "target_device_id": r.target_device_id,
        "created_at": iso_utc(r.created_at),
    }


# ---------------------------------------------------------------------------
# 云手机文件预览：选择云手机，浏览其内部文件系统并预览内容
# ---------------------------------------------------------------------------
IMG_EXT_DEV = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico"}


def _dev_or_404(did):
    dev = db.session.get(Device, int(did))
    if dev is None:
        return None, (jsonify(error="设备不存在"), 404)
    return dev, None


@files_bp.get("/files/devices/<int:did>/fs")
@login_required
def device_fs(did):
    """列出指定云手机某目录的文件。"""
    dev, err = _dev_or_404(did)
    if err:
        return err
    path = request.args.get("path") or "/sdcard/"
    return jsonify(list_device_dir(dev.serial, path))


@files_bp.get("/files/devices/<int:did>/fs/read")
@login_required
def device_fs_read(did):
    """读取云手机上文本文件内容（前 150KB，用于预览）。"""
    dev, err = _dev_or_404(did)
    if err:
        return err
    path = request.args.get("path")
    if not path:
        return jsonify(error="缺少 path 参数"), 400
    return jsonify(read_device_file(dev.serial, path))


@files_bp.get("/files/devices/<int:did>/fs/file")
@login_required
def device_fs_file(did):
    """拉取云手机文件到本地（图片预览 / 下载），返回文件流。"""
    dev, err = _dev_or_404(did)
    if err:
        return err
    path = request.args.get("path")
    if not path:
        return jsonify(error="缺少 path 参数"), 400
    tmp_dir = _adb_ascii_dir()
    import time as _t
    fname = os.path.basename(path) or f"dev_{_t.time()}"
    local = os.path.join(tmp_dir, f"dev_{int(_t.time()*1000)}_{fname}")
    try:
        res = pull_file_from_device(dev.serial, path, local, timeout=120)
        if not res.get("ok") or not os.path.isfile(local):
            return jsonify(error=f"拉取失败: {res.get('output', '')[:120]}"), 400
        # 由 send_file 负责回传，调用方负责清理
        resp = send_file(local, as_attachment=False, download_name=fname)
        resp.call_on_close(lambda: _try_remove(local))
        return resp
    except Exception as e:  # noqa: BLE001
        _try_remove(local)
        return jsonify(error=f"拉取失败: {e}"), 400


def _try_remove(p):
    try:
        if os.path.isfile(p):
            os.remove(p)
    except OSError:
        pass
