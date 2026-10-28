"""稳定启动入口：debug 开启但禁用自动 reloader，避免反复重启导致进程退出。

用法：python run_stable.py
"""
import os
from app import create_app

app = create_app()

# 启动自检：给已有表补齐模型里新加的列。
# 不补的话，备份恢复来的库会因缺列让相关接口直接 500。
from app.schema_guard import ensure_schema  # noqa: E402

ensure_schema(app)

# 多企业：建默认企业，并把 enterprise_id 为空的历史数据挂到默认企业下（幂等）
with app.app_context():
    try:
        from app.tenant import ensure_default_enterprise

        _ent = ensure_default_enterprise()
        print(f"[tenant] 默认企业: {_ent.name}（#{_ent.id}）", flush=True)
    except Exception as _e:  # noqa: BLE001 - 企业初始化失败不应阻塞启动
        print(f"[tenant] 初始化失败: {_e}", flush=True)

# —— 全局请求并发上限：防止任何客户端风暴把线程/连接打爆（线程无上限是积压根因之一）——
import datetime as _dt


def _now_utc():
    """当前 naive UTC 时间（库里存的就是这个口径）。"""
    return _dt.datetime.utcnow()


import threading as _threading
from flask import g as _g, jsonify as _jsonify

_MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "240"))
_request_sem = _threading.BoundedSemaphore(_MAX_CONCURRENT)


@app.before_request
def _acquire_request_slot():
    _g._req_slot = False
    if _request_sem.acquire(blocking=False):
        _g._req_slot = True
    else:
        return _jsonify(error="系统繁忙，请稍后重试"), 503


@app.teardown_request
def _release_request_slot(exc):  # noqa: ARG001
    if getattr(_g, "_req_slot", False):
        try:
            _request_sem.release()
        except ValueError:
            pass


def _start_tunnel():
    import subprocess, sys
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        import socket
        _probe = socket.socket(); _probe.settimeout(1)
        if _probe.connect_ex(("127.0.0.1", 6000)) == 0:
            print("adb_tunnel already running, skip")
            return
        _probe.close()
        logf = open(os.path.join(base, "adb_tunnel.log"), "a", encoding="utf-8")
        flags = 0x00000008 if os.name == "nt" else 0
        subprocess.Popen([sys.executable, os.path.join(base, "app", "adb_tunnel.py")],
                         stdout=logf, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True)
        print("adb_tunnel started")
    except Exception as e:
        print("adb_tunnel start fail:", e)

if os.environ.get("SSH_TUNNEL_ENABLED", "").lower() == "true":
    _start_tunnel()


def _start_adb_warmup(app):
    """后台预热所有云手机 adb 连接（分批错峰），避免前端首屏截图并发首连失败。"""
    import threading

    def _run():
        try:
            with app.app_context():
                from app.models import Device
                serials = [d.serial for d in db_query_devices()]
            if not serials:
                print("adb warmup: no devices, skip")
                return
            from app.orchestrator import warmup_adb_connections
            ok, failed = warmup_adb_connections(serials)
            msg = f"adb warmup done: {ok}/{len(serials)} connected"
            if failed:
                msg += f", failed {len(failed)}: {failed[:5]}"
            print(msg)
        except Exception as e:  # noqa: BLE001
            print("adb warmup fail:", e)

    threading.Thread(target=_run, daemon=True).start()


def db_query_devices():
    from app.models import Device
    from app.extensions import db
    return db.session.query(Device).all()


def _restore_relays(app):
    """后端重启后重建 SSH 中继通道。

    中继通道活在后端进程里，一重启就全断，库中那些 127.0.0.1:36xxx 的设备
    会全部离线。这里按已登记服务器重新发现一遍把通道建回来，
    免得管理员每次重启都要手动去界面上重新扫描。
    """
    import threading
    import time

    def _run():
        time.sleep(3)
        try:
            with app.app_context():
                from app.models import Server, Device
                from app.extensions import db
                hosts = [r[0] for r in db.session.query(Device.ip).filter(
                    Device.serial.like("127.0.0.1:36%")).distinct().all()]
                servers = (db.session.query(Server).filter(Server.host.in_(hosts)).all()
                           if hosts else [])
            if not servers:
                return
            import json as _json
            from app.routes.servers import _extract_params, _attach_relays, _adb_connect
            from app import ssh_probe
            for s in servers:
                try:
                    with app.app_context():
                        from app.extensions import db as _db
                        params = _extract_params({}, s.to_dict(with_secret=True))
                        r = ssh_probe.probe_server(params, probe_models=False)
                        _attach_relays(r, params)
                        by_remote = {d.get("serial"): d for d in r.get("devices", [])}
                        serials = [d.get("local_serial") for d in r.get("devices", [])
                                   if d.get("local_serial")]
                        # 关键：库里存的是上一次进程分配的中继端口，重启后端口会变，
                        # 不同步的话设备会全部显示 error（库存也跟着掉）。
                        fixed = 0
                        # local_serial -> 服务器侧 serial：给还没有 remote_serial 的设备补上，
                        # 投屏（ws-scrcpy）和重启后重建中继都依赖这个值
                        by_local = {d.get("local_serial"): d.get("serial")
                                    for d in r.get("devices", []) if d.get("local_serial")}

                        # 两阶段改 serial：重启后端口是重新分配的，直接批量 UPDATE 会出现
                        # "A 要改成 B 现在占着的端口" 的中间态，撞 devices(backend, serial)
                        # 唯一约束（实测 122 整批恢复失败）。先统一改成占位值，再写终值。
                        pending = []
                        for dev in db.session.query(Device).filter(Device.ip == s.host).all():
                            try:
                                fp = _json.loads(dev.fingerprint or "{}")
                            except ValueError:
                                fp = {}
                            if not isinstance(fp, dict):
                                fp = {}
                            remote = fp.get("remote_serial")
                            new_ser = (by_remote.get(remote) or {}).get("local_serial") if remote else None
                            if new_ser and dev.serial != new_ser:
                                pending.append((dev.id, new_ser))
                                dev.serial = f"relay-pending:{dev.id}"
                                fixed += 1
                            # 补 remote_serial（投屏 udid 用）
                            if not remote or remote not in by_remote:
                                cand = by_local.get(dev.serial) or by_local.get(new_ser or "")
                                if cand:
                                    fp["remote_serial"] = cand
                                    dev.fingerprint = _json.dumps(fp, ensure_ascii=False)
                                    fixed += 1
                        if pending:
                            _db.session.commit()      # 先释放旧端口
                            for dev_id, ser in pending:
                                d2 = _db.session.get(Device, dev_id)
                                if d2 is None:
                                    continue
                                d2.serial = ser
                                d2.status = "running"
                                d2.last_seen = _now_utc()
                            _db.session.commit()
                        elif fixed:
                            _db.session.commit()
                    for ser in serials:
                        _adb_connect(ser)
                    print(f"[relay] 恢复 {s.host}: {len(serials)} 台，更正 serial {fixed} 台", flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"[relay] 恢复 {s.host} 失败: {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            print("[relay] 恢复失败:", e, flush=True)

    threading.Thread(target=_run, name="relay-restore", daemon=True).start()


_restore_relays(app)


if os.environ.get("ADB_WARMUP_ENABLED", "true").lower() == "true":
    _start_adb_warmup(app)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8001"))
        # use werkzeug run_simple for single-process serving (avoid werkzeug subprocess)
    from werkzeug.serving import run_simple
    run_simple("0.0.0.0", port, app, threaded=True, use_reloader=False)
