# -*- coding: utf-8 -*-
"""开通后的后台任务队列：设备初始化 / 装 APK / 套桌面模板。

方案 2：
- /internal/provision 保持同步、快速返回（只做池分配 + 建订阅）；
- 装 APK、套桌面模板等慢操作入队，由 APScheduler 每 5 秒取一批执行；
- 失败指数退避重试，超过 max_attempts 进死信（status=dead）。
"""
import json
import os
import subprocess
import time
from datetime import datetime, timedelta

from flask import current_app

from .extensions import db
from .models import Device, ProvisionJob

LAUNCHER_PKG = "com.launcheros15.ilauncher"


def enqueue(kind, payload, order_item_id=None):
    """入队一条任务（不 commit，由调用方统一提交）。"""
    job = ProvisionJob(
        kind=kind,
        payload=json.dumps(payload or {}, ensure_ascii=False),
        order_item_id=order_item_id,
        status="pending",
        attempts=0,
        next_run_at=datetime.utcnow(),
    )
    db.session.add(job)
    return job


def tick(limit=3):
    """取一批到点的 pending 任务执行。由 scheduler 每 5 秒调用。"""
    now = datetime.utcnow()
    jobs = (
        db.session.query(ProvisionJob)
        .filter(ProvisionJob.status == "pending", ProvisionJob.next_run_at <= now)
        .order_by(ProvisionJob.id)
        .limit(limit)
        .all()
    )
    for job in jobs:
        _run(job)
    return len(jobs)


def _run(job):
    job.status = "running"
    job.attempts = int(job.attempts or 0) + 1
    db.session.commit()
    try:
        handler = HANDLERS.get(job.kind)
        if handler is None:
            raise RuntimeError("unknown job kind: %s" % job.kind)
        handler(json.loads(job.payload or "{}"))
        job.status = "success"
        job.last_error = ""
    except Exception as e:  # noqa: BLE001
        job.last_error = str(e)[:500]
        if int(job.attempts or 0) >= int(job.max_attempts or 5):
            job.status = "dead"
        else:
            job.status = "pending"
            delay = min(300, 15 * (2 ** max(0, int(job.attempts or 1) - 1)))
            job.next_run_at = datetime.utcnow() + timedelta(seconds=delay)
    db.session.commit()


# ---------------- 具体的后台动作 ----------------

def _adb(*args, timeout=60):
    """直接调 adb（后端宿主机上执行）。"""
    from . import orchestrator
    adb = orchestrator._adb_path()
    return subprocess.run([adb] + [str(a) for a in args],
                          capture_output=True, text=True, timeout=timeout)


def _apply_launcher_template(serial, tpl_dir):
    """把模板目录里的 prefs.xml + icons/ 套到设备桌面。

    模板目录约定：
      <tpl_dir>/prefs.xml   → 共享首选项（arr_home 等布局键）
      <tpl_dir>/icons/      → 图标缓存 jpg
    """
    prefs = os.path.join(tpl_dir, "prefs.xml")
    icons = os.path.join(tpl_dir, "icons")
    _adb("-s", serial, "root", timeout=20)
    time.sleep(1)
    uid = _adb("-s", serial, "shell", "stat", "-c", "%u",
               "/data/data/" + LAUNCHER_PKG, timeout=20).stdout.strip()
    if os.path.isfile(prefs):
        _adb("-s", serial, "push", prefs, "/data/local/tmp/sharedpreferences.xml", timeout=120)
        if uid:
            _adb("-s", serial, "shell", "am force-stop " + LAUNCHER_PKG, timeout=20)
            _adb("-s", serial, "shell",
                 "cp /data/local/tmp/sharedpreferences.xml /data/data/%s/shared_prefs/sharedpreferences.xml && "
                 "chown %s:%s /data/data/%s/shared_prefs/sharedpreferences.xml && "
                 "chmod 660 /data/data/%s/shared_prefs/sharedpreferences.xml"
                 % (LAUNCHER_PKG, uid, uid, LAUNCHER_PKG, LAUNCHER_PKG), timeout=30)
    if os.path.isdir(icons) and uid:
        _adb("-s", serial, "push", icons, "/data/local/tmp/", timeout=180)
        ext = "/storage/emulated/0/Android/data/%s/files/icon_app" % LAUNCHER_PKG
        _adb("-s", serial, "shell",
             "cp -r /data/local/tmp/%s/. %s/ && chown -R %s:%s %s/"
             % (os.path.basename(icons), ext, uid, uid, ext), timeout=60)
    _adb("-s", serial, "shell",
         "am start -a android.intent.action.MAIN -c android.intent.category.HOME", timeout=20)


def _device_init(payload):
    """设备初始化：探活 → 装 APK（可选）→ 套桌面模板（可选）。"""
    device_ids = payload.get("device_ids") or []
    apk = (current_app.config.get("PROVISION_INIT_APK") or "").strip()
    tpl = (current_app.config.get("PROVISION_INIT_TEMPLATE_DIR") or "").strip()
    errors = []
    for did in device_ids:
        try:
            d = db.session.get(Device, int(did))
        except (TypeError, ValueError):
            continue
        if d is None or not d.serial:
            continue
        if d.backend == "simulator":
            continue
        try:
            from . import orchestrator
            orchestrator.get_device_info(d.serial)
            if apk and os.path.isfile(apk):
                r = orchestrator.install_apk(d.serial, apk) or {}
                if r.get("ok") is False:
                    raise RuntimeError("install_apk: %s" % (r.get("error") or r))
            if tpl and os.path.isdir(tpl):
                _apply_launcher_template(d.serial, tpl)
        except Exception as e:  # noqa: BLE001
            errors.append("%s: %s" % (did, e))
    if errors:
        raise RuntimeError("; ".join(errors)[:400])


HANDLERS = {
    "device_init": _device_init,
}
