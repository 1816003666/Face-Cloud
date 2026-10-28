"""后台任务调度器（APScheduler）。

- 每 5 秒扫描「enabled 且 next_run 到点」的任务，先重排排期再执行（防崩溃重复触发）；
- execute_task 既被调度器调用，也被手动「立即执行」接口调用；
- 任务动作目前以 simulator 记录日志为主，真实动作在 orchestrator 扩展。
"""
import json
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

_scheduler = None
_app = None


def start_scheduler(app):
    global _scheduler, _app
    _app = app
    if _scheduler is not None:
        return  # 避免重复启动（如 reload）
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_tick, "interval", seconds=5, id="task_tick")
    # 告警系统健康检查：设备离线 / 资源超限（任务书 #12）
    _scheduler.add_job(run_alert_checks, "interval", seconds=30, id="alert_checks")
    # 云手机轮次运行检查：到点销毁并重建所有 redroid 设备
    _scheduler.add_job(rotation_check, "interval", seconds=60, id="rotation_check")
    # 普通用户的轮次运行：到点清数据收回并重新分配
    _scheduler.add_job(user_rotation_check, "interval", seconds=60, id="user_rotation_check")
    _scheduler.add_job(snapshot_metrics, "interval", seconds=60, id="metrics_snapshot")
    # 预付费计费：套餐到期提醒（前 3 天 / 当天）+ 到期断机（每 60s 扫描）
    _scheduler.add_job(billing_check, "interval", seconds=60, id="billing_check")
    # 开通后的后台初始化（装 APK / 套桌面模板），见 provision_queue.py
    _scheduler.add_job(_provision_tick, "interval", seconds=5, id="provision_jobs_tick")
    # 待支付订单超时自动取消（默认 30 分钟，每 60 秒扫一次）
    _scheduler.add_job(_order_timeout_tick, "interval", seconds=60, id="order_timeout")
    _scheduler.start()


def _now():
    return datetime.utcnow()


def _provision_tick():
    if _app is None:
        return
    with _app.app_context():
        try:
            from .provision_queue import tick
            tick()
        except Exception as e:  # noqa: BLE001
            print(f"[provision_queue] tick 异常: {e}")


def _order_timeout_tick():
    if _app is None:
        return
    with _app.app_context():
        try:
            from .billing import cancel_stale_pending_orders
            n = cancel_stale_pending_orders()
            if n:
                print(f"[order] 自动取消 {n} 个超时未支付订单")
        except Exception as e:  # noqa: BLE001
            print(f"[order] 超时取消失败: {e}")


def _tick():
    if _app is None:
        return
    with _app.app_context():
        from .extensions import db
        from .models import ScheduledTask
        due = db.session.query(ScheduledTask).filter(
            ScheduledTask.enabled.is_(True),
            ScheduledTask.next_run <= _now(),
        ).all()
        for t0 in due:
            try:
                # 重新从 DB 获取，避免任务被并发删除后访问已删实例
                t = db.session.get(ScheduledTask, t0.id)
                if t is None or not t.enabled:
                    continue
                execute_task(t, actor_id=t.created_by)
                # 重排下次执行时间（先排期再跑，防重复）
                if t.schedule_type == "interval":
                    t.next_run = _now() + timedelta(seconds=t.interval_seconds)
                elif t.schedule_type == "cron":
                    from croniter import croniter
                    t.next_run = croniter(t.cron_expr or "0 * * * *",
                                          _now()).get_next(datetime)
                else:
                    t.enabled = False  # once 任务执行后禁用
                db.session.commit()
            except Exception as e:  # noqa: BLE001
                db.session.rollback()
                print(f"[tick] 任务 {t0.id} 处理异常: {e}")


def execute_task(task, actor_id: int, device_ids_override=None) -> int:
    """执行一个任务，写一条 TaskExecution 记录。返回 execution id。"""
    from .extensions import db
    from .models import TaskExecution, Device, record_audit

    # 健康巡检：整机巡检一次（服务器 + 云手机），不逐设备重复执行
    if task.action == "health_check":
        with _TASK_EXEC_LOCK:
            return _run_health_check(task, actor_id)

    # device_ids_override：只跑指定的设备（「重跑失败设备」用）
    device_ids = (list(device_ids_override) if device_ids_override is not None
                  else (json.loads(task.device_ids) if task.device_ids else []))
    params = json.loads(task.params) if task.params else {}
    exec_rec = TaskExecution(task_id=task.id, status="running",
                             total=len(device_ids))
    db.session.add(exec_rec)
    db.session.commit()

    ok = failed = 0
    details = []
    devices = db.session.query(Device).filter(Device.id.in_(device_ids)).all() \
        if device_ids else []

    # 没有任何设备可执行时必须显式失败：
    # 以前这里 total=0/ok=0/failed=0 一律写 success，界面上就是"执行成功但什么也没发生"。
    if not devices:
        exec_rec.status = "failed"
        exec_rec.finished_at = _now()
        exec_rec.detail = json.dumps(
            [{"ok": False, "error": "任务没有选择云手机，未执行任何操作"}], ensure_ascii=False)
        db.session.commit()
        record_audit(actor_id, "execute_task", "task", task.id,
                     {"ok": 0, "failed": 0, "error": "no devices"})
        return exec_rec.id

    # 执行动作/参数可被「执行脚本」改写：run_script 最终按脚本里的 steps 当 sequence 跑
    run_action = task.action
    run_params = params

    # 「安装应用」不在 control_device 里实现，这里走真实 adb install
    apk_path = None
    if task.action == "install":
        from .models import FileRecord
        import os as _os
        from .routes.files import _upload_dir
        rec = db.session.get(FileRecord, int(params.get("file_id") or 0))
        if rec is None:
            apk_path = None
        else:
            apk_path = _os.path.join(_upload_dir(), rec.stored_name)

    if task.action == "run_script":
        # 脚本校验失败要在写执行记录之前处理：否则异常会一路抛到路由层，
        # 留下一条永远 "running" 的孤儿记录。
        from .models import Script
        script = db.session.get(Script, int(params.get("script_id") or 0))
        if script is None:
            exec_rec.status = "failed"
            exec_rec.finished_at = _now()
            exec_rec.detail = json.dumps(
                [{"ok": False, "error": "脚本不存在（可能已被删除），请重新选择"}], ensure_ascii=False)
            db.session.commit()
            record_audit(actor_id, "execute_task", "task", task.id,
                         {"ok": 0, "failed": 0, "error": "script missing"})
            return exec_rec.id
        try:
            steps = json.loads(script.steps or "[]")
        except (TypeError, ValueError):
            steps = []
        if not steps:
            exec_rec.status = "failed"
            exec_rec.finished_at = _now()
            exec_rec.detail = json.dumps(
                [{"ok": False, "error": f"脚本「{script.name}」没有任何步骤"}], ensure_ascii=False)
            db.session.commit()
            record_audit(actor_id, "execute_task", "task", task.id,
                         {"ok": 0, "failed": 0, "error": "empty script"})
            return exec_rec.id
        # 变量：脚本默认值 ← 任务里 params.variables 覆盖
        from .routes.scripts import _script_variables, _render
        run_vars = _script_variables(script)
        passed = params.get("variables") or {}
        if isinstance(passed, dict):
            run_vars.update({str(k): v for k, v in passed.items()})
        steps = _render(steps, run_vars)
        run_action = "sequence"
        run_params = {"steps": steps}

    max_retries = max(0, int(task.max_retries or 0))
    from . import orchestrator

    def _do_one(d):
        if run_action == "install":
            if not apk_path or not _os.path.isfile(apk_path):
                raise RuntimeError("APK 文件不存在（可能已被删除），请重新选择")
            return orchestrator.install_apk_to_device(d.serial, apk_path)
        return orchestrator.control_device(d.backend, d.serial, run_action, run_params)

    with _TASK_EXEC_LOCK:
        for d in devices:
            attempts = 0
            while True:
                try:
                    res = _do_one(d)
                    if isinstance(res, dict) and res.get("ok") is False:
                        raise RuntimeError(str(res.get("result") or "执行失败")[:200])
                    ok += 1
                    details.append({"device_id": d.id, "ok": True,
                                    "attempts": attempts + 1})
                    break
                except Exception as e:  # noqa: BLE001
                    if attempts < max_retries:
                        attempts += 1
                        _time.sleep(1)
                        continue
                    failed += 1
                    details.append({"device_id": d.id, "ok": False,
                                    "error": str(e)[:200], "attempts": attempts + 1})
                    break

    # 重新获取，防止任务被并发删除时级联删除了 exec_rec 导致 ObjectDeletedError
    exec_rec = db.session.get(TaskExecution, exec_rec.id)
    if exec_rec is None:
        return -1  # 执行记录已被删除，直接返回

    exec_rec.ok = ok
    exec_rec.failed = failed
    exec_rec.status = "success" if failed == 0 else "failed"
    exec_rec.finished_at = _now()
    exec_rec.detail = json.dumps(details, ensure_ascii=False)
    db.session.commit()
    record_audit(actor_id, "execute_task", "task", task.id,
                 {"ok": ok, "failed": failed})
    # 任务失败（哪怕只挂一台）给归属人发站内通知，别让定时任务默默失败
    if failed > 0 and task.created_by:
        try:
            from . import billing
            billing._notify(task.created_by, "task_failed", "任务执行失败",
                            f"任务「{task.name}」执行失败：成功 {ok}/{len(devices)} 台，失败 {failed} 台。"
                            f"可在「任务调度 → 历史」查看失败原因。")
        except Exception as e:  # noqa: BLE001 - 通知失败不影响任务结果
            print(f"[scheduler] 任务失败通知发送失败 task={task.id}: {e}", flush=True)
    return exec_rec.id


# ---- 资源采集降频/并发（2026-09-18 修复：避免巡检长事务拖垮控制接口）----
import time as _time
import threading as _threading
# adb server 是单点：同一时刻只允许一个任务在跑，避免并发执行把 adb 打挂
_TASK_EXEC_LOCK = _threading.Lock()
_RESOURCE_INTERVAL = 180.0          # 资源（CPU/MEM）采集最小间隔（秒）
_RESOURCE_CONCURRENCY = 12          # 采集并发上限（adb server 单点，不宜过大）
_last_resource_at = 0.0


# 每轮最多重连多少台离线设备（轮转，避免一次打满 adb）
_RECONNECT_BUDGET = 12
_RECONNECT_WORKERS = 6
_reconnect_cursor = 0


def _safe_adb_connect(serial):
    try:
        from . import orchestrator
        return bool(orchestrator.adb_connect(serial))
    except Exception:  # noqa: BLE001
        return False


def _reconnect_some(serials, budget=_RECONNECT_BUDGET):
    """每轮只重连一小批离线设备（游标轮转 + 并发），保证巡检不被 adb 拖死。"""
    global _reconnect_cursor
    if not serials:
        return
    lst = sorted(set(serials))
    if len(lst) <= budget:
        picked = lst
        _reconnect_cursor = 0
    else:
        start = _reconnect_cursor % len(lst)
        picked = [lst[(start + i) % len(lst)] for i in range(budget)]
        _reconnect_cursor = (start + budget) % len(lst)
    try:
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=min(_RECONNECT_WORKERS, len(picked))) as ex:
            list(ex.map(_safe_adb_connect, picked))
    except Exception:  # noqa: BLE001
        pass


def _sample_device_resources(d):
    """在巡检线程外采集单台 redroid 的 CPU/MEM（通过 /proc）。返回 (cpu, mem)。"""
    from . import orchestrator
    cpu = mem = None
    try:
        rc, out, _ = orchestrator._run_adb(
            ["-s", d.serial, "shell", "cat", "/proc/stat"], timeout=5)
        if out:
            parts = out.decode().split()
            if len(parts) >= 5:
                user, nice, sys, idle = int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4])
                total = user + nice + sys + idle
                if total > 0:
                    cpu = round((user + nice + sys) / total * 100, 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        rc, out, _ = orchestrator._run_adb(
            ["-s", d.serial, "shell", "cat", "/proc/meminfo"], timeout=5)
        if out:
            lines = out.decode().splitlines()
            total = avail = 0
            for line in lines:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1])
            if total > 0:
                mem = round((1 - avail / total) * 100, 1)
    except Exception:  # noqa: BLE001
        pass
    return cpu, mem


def run_alert_checks():
    """告警健康检查（每 30s）。

    - device_offline：running 设备超过 ALERT_OFFLINE_SECONDS 无心跳 → 置 error + 告警；
    - resource_limit：running 设备 CPU/内存超过阈值 → 告警（阈值由 config 控制）。

    两阶段实现，避免超长写事务把控制接口（UPDATE last_seen）拖成 501：
      阶段一（不碰 ORM）：adb 在线列表 + 资源采样（并发、降频）；
      阶段二（快速写库）：统一应用状态 + 一次性 commit，告警在 commit 后再独立写。
    """
    global _last_resource_at
    if _app is None:
        return
    with _app.app_context():
        from .extensions import db
        from .models import Device, raise_alert, _utcnow, get_setting
        cfg = _app.config
        def _g(key, fallback):
            try:
                v = get_setting(key)
                return int(v) if v is not None else fallback
            except Exception:  # noqa: BLE001
                return fallback
        offline_sec = _g("alert_offline_seconds", cfg.get("ALERT_OFFLINE_SECONDS", 120))
        cpu_limit = _g("alert_cpu_limit", cfg.get("ALERT_CPU_LIMIT", 90))
        mem_limit = _g("alert_mem_limit", cfg.get("ALERT_MEM_LIMIT", 90))
        now = _utcnow()

        devices = db.session.query(Device).all()
        has_redroid = any(d.backend == "redroid" for d in devices)

        # ---- 阶段一：采样（不修改 ORM，不触发 autoflush 写库）----
        online_serials = set()
        if has_redroid:
            try:
                from . import orchestrator
                adb_devs = orchestrator.list_adb_devices()
                online_serials = {x["serial"] for x in adb_devs if x["status"] == "device"}
            except Exception:  # noqa: BLE001
                pass

        do_resource = (_time.monotonic() - _last_resource_at) >= _RESOURCE_INTERVAL
        if do_resource:
            _last_resource_at = _time.monotonic()

        # 需要采集资源的 redroid（在线且本轮到点）
        to_sample = []
        for d in devices:
            if d.backend == "redroid" and d.serial in online_serials and do_resource:
                to_sample.append(d)

        resource_map = {}
        if to_sample:
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=_RESOURCE_CONCURRENCY) as ex:
                futs = {ex.submit(_sample_device_resources, d): d for d in to_sample}
                for f in _cf.as_completed(futs):
                    d = futs[f]
                    try:
                        resource_map[d.id] = f.result()
                    except Exception:  # noqa: BLE001
                        resource_map[d.id] = (None, None)

        # ---- 阶段二：应用状态 + 快速提交（不在循环内独立 commit）----
        resolved_ids = []
        alert_queue = []
        with db.session.no_autoflush:
            for d in devices:
                if d.backend == "simulator":
                    # simulator 是纯内存 Mock，自动刷新心跳避免误判
                    d.last_seen = now
                    d.cpu = 20.0 + (d.id * 13) % 70      # 20 ~ 89
                    d.mem = 30.0 + (d.id * 11) % 60      # 30 ~ 89
                    if d.status != "running":
                        d.status = "running"
                elif d.backend == "redroid":
                    if d.serial in online_serials:
                        # ADB 在线：更新心跳 + 恢复状态（如果之前是 error）
                        was_offline = d.status != "running"
                        d.last_seen = now
                        if was_offline:
                            d.status = "running"
                            resolved_ids.append(d.id)
                        cpu, mem = resource_map.get(d.id, (None, None))
                        if cpu is not None:
                            d.cpu = cpu
                        if mem is not None:
                            d.mem = mem

                # 离线判定：running 设备超过 offline_sec 无心跳 → 置 error
                if d.status == "running" and d.last_seen and (now - d.last_seen).total_seconds() > offline_sec:
                    d.status = "error"
                    alert_queue.append(("device_offline", "critical",
                                        f"设备 {d.name} 离线超过 {offline_sec}s（最后心跳 {d.last_seen.isoformat()}）",
                                        d.id, None))
                elif d.status == "running" and d.cpu and d.cpu > cpu_limit:
                    alert_queue.append(("resource_limit", "warning",
                                        f"设备 {d.name} CPU {d.cpu:.0f}% 超过阈值 {cpu_limit:.0f}%",
                                        d.id, {"cpu": d.cpu, "mem": d.mem}))
                elif d.status == "running" and d.mem and d.mem > mem_limit:
                    alert_queue.append(("resource_limit", "warning",
                                        f"设备 {d.name} 内存 {d.mem:.0f}% 超过阈值 {mem_limit:.0f}%",
                                        d.id, {"cpu": d.cpu, "mem": d.mem}))

        # 状态/心跳统一落库（快速，不在 adb 采样期间持有写事务）
        try:
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()
            return

        # ---- 阶段三：离线设备重连（限流轮转）----
        # 必须放在状态落库**之后**、且每轮限量：对所有离线设备串行 adb connect 时，
        # 每台最多耗 10s，200+ 台一轮要几十分钟，状态迟迟更新不了 →
        # 前端一直以为设备 running 而持续请求截图，最终打满并发上限（503）并大量 502。
        offline_serials = [d.serial for d in devices
                           if d.backend == "redroid" and ":" in (d.serial or "")
                           and d.serial not in online_serials]
        if offline_serials:
            _reconnect_some(offline_serials)

        # 告警与离线恢复在 commit 后独立处理（raise_alert/resolve 内部自带 commit）
        if resolved_ids:
            try:
                from .models import resolve_alerts_for_device
                for did in resolved_ids:
                    try:
                        n = resolve_alerts_for_device(did, "device_offline")
                        if n:
                            dev = db.session.get(Device, did)
                            print(f"[alert] 设备 {dev.name if dev else did} 在线，自动解决 {n} 条离线告警")
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
        for atype, level, msg, did, detail in alert_queue:
            try:
                raise_alert(atype, level, msg, device_id=did, detail=detail)
            except Exception:  # noqa: BLE001
                db.session.rollback()


def user_rotation_check():
    """普通用户轮次运行：到点把用户名下设备清数据收回并重新分配。"""
    if _app is None:
        return
    from . import user_rotation
    try:
        user_rotation.tick(_app)
    except Exception as e:  # noqa: BLE001
        print(f"[rotation] 用户轮次检查失败: {e}", flush=True)


def rotation_check():
    """云手机轮次运行：开启后每轮结束（24h / rounds）自动销毁并重建所有 redroid 设备。

    每 60 秒检查一次 next_round_at 是否到点；到点执行轮转并排期下一轮。
    """
    if _app is None:
        return
    with _app.app_context():
        from .extensions import db
        from . import orchestrator
        from datetime import timedelta
        from .models import RotationConfig

        cfg = db.session.get(RotationConfig, 1)
        if cfg is None or not cfg.enabled or not cfg.next_round_at:
            return
        now = _now()
        if now < cfg.next_round_at:
            return

        # 到点：销毁并重建所有 redroid 云手机
        result = orchestrator.rotate_redroid_devices()
        # 排期下一轮
        cfg.round_index = (cfg.round_index % cfg.rounds) + 1
        cfg.next_round_at = _now() + timedelta(hours=24 / cfg.rounds)
        db.session.commit()
        print(f"[rotation] round {cfg.round_index} rotated: "
              f"destroyed={result.get('destroyed')} created={result.get('created')} "
              f"failed={result.get('failed')}")


# ---------------------------------------------------------------------------
# 健康巡检任务（health_check）
# 每天巡检一次：服务器（CPU/内存/磁盘/容器）+ 云手机（ADB 在线/状态/资源）
# 结果写入 TaskExecution.detail，异常触发告警。
# ---------------------------------------------------------------------------
def _run_health_check(task, actor_id: int) -> int:
    from .extensions import db
    from .models import TaskExecution, Device, raise_alert, record_audit

    exec_rec = TaskExecution(task_id=task.id, status="running", total=0)
    db.session.add(exec_rec)
    db.session.commit()

    report = {
        "type": "health_check",
        "generated_at": _now().isoformat(),
        "server": _collect_server_health(),
        "devices": {"total": 0, "ok": 0, "failed": 0, "items": []},
    }

    # —— 云手机健康 ——
    from . import orchestrator
    try:
        adb_devs = orchestrator.list_adb_devices()
        online_serials = {x["serial"] for x in adb_devs if x["status"] == "device"}
    except Exception:
        online_serials = set()

    from .models import get_setting
    try:
        cpu_limit = int(get_setting("alert_cpu_limit") or 90)
        mem_limit = int(get_setting("alert_mem_limit") or 90)
    except Exception:  # noqa: BLE001
        cpu_limit = mem_limit = 90
    # —— 兜底：巡检只统计「任务归属人」能看到的设备 ——
    # API 层已限制「健康巡检仅管理员」，但调度器仍可能自动执行历史遗留任务，
    # 这里再按 owner 范围过滤一次，避免越权数据继续写进执行记录。
    from .models import User as _User
    from . import scoping as _scoping
    _q = db.session.query(Device).filter(Device.backend == "redroid")
    _owner = db.session.get(_User, task.created_by) if task.created_by else None
    if _owner is not None:
        _q = _scoping.scope_query(_q, _owner)
    else:
        _q = _q.filter(Device.id < 0)   # 无归属人 → 视为无权限，不巡检任何设备
    dev_rows = _q.all()
    dev_ok = dev_failed = 0
    for d in dev_rows:
        issues = []
        online = d.serial in online_serials
        if not online:
            issues.append("ADB 离线")
        if d.status != "running":
            issues.append(f"状态={d.status}")
        if d.cpu is not None and d.cpu > cpu_limit:
            issues.append(f"CPU {d.cpu:.0f}%")
        if d.mem is not None and d.mem > mem_limit:
            issues.append(f"内存 {d.mem:.0f}%")
        is_ok = len(issues) == 0
        if is_ok:
            dev_ok += 1
        else:
            dev_failed += 1
        report["devices"]["items"].append({
            "device_id": d.id, "name": d.name, "serial": d.serial,
            "status": d.status, "online": online, "cpu": d.cpu, "mem": d.mem,
            "ok": is_ok, "issues": issues,
        })
    report["devices"]["total"] = len(dev_rows)
    report["devices"]["ok"] = dev_ok
    report["devices"]["failed"] = dev_failed

    # —— 汇总 & 告警 ——
    server_ok = report["server"].get("ok", False)
    server_issues = report["server"].get("issues", [])
    total_issues = len(server_issues) + dev_failed
    if total_issues == 0:
        summary = f"健康：服务器正常，云手机 {dev_ok}/{len(dev_rows)} 在线"
    else:
        summary = f"存在 {total_issues} 项异常：服务器 {len(server_issues)} 项，云手机 {dev_failed}/{len(dev_rows)} 台"
    report["summary"] = summary

    status = "success" if total_issues == 0 else "failed"
    exec_rec = db.session.get(TaskExecution, exec_rec.id)
    if exec_rec is not None:
        exec_rec.status = status
        exec_rec.total = len(dev_rows)
        exec_rec.ok = dev_ok
        exec_rec.failed = dev_failed
        exec_rec.finished_at = _now()
        exec_rec.detail = json.dumps(report, ensure_ascii=False, default=str)
        db.session.commit()
        record_audit(actor_id, "execute_task", "task", task.id,
                     {"action": "health_check", "ok": dev_ok, "failed": dev_failed,
                      "server_ok": server_ok})

    if total_issues > 0:
        raise_alert("health_check", "critical" if (not server_ok or dev_failed) else "warning",
                    summary, detail={"server_issues": server_issues,
                                     "device_failed": dev_failed})
    return exec_rec.id if exec_rec is not None else -1


def _collect_server_health() -> dict:
    """采集云手机服务器（SSH）的 CPU 负载 / 内存 / 磁盘 / Docker 容器。"""
    from . import orchestrator
    from .orchestrator import _ssh_enabled, _ssh_connect, _ssh_exec

    res = {"ok": False, "reachable": False, "host": orchestrator._server_ip(),
           "metrics": {}, "issues": []}
    if not _ssh_enabled():
        res["issues"].append("SSH 未配置（无法巡检服务器）")
        return res

    client = None
    try:
        client = _ssh_connect()
        res["reachable"] = True
        # 核心数
        rc, out, _ = _ssh_exec(client, "nproc")
        cores = int(out.strip() or 1) if rc == 0 else 1
        # 负载
        rc, out, _ = _ssh_exec(client, "cat /proc/loadavg")
        load = float(out.split()[0]) if rc == 0 and out.strip() else 0.0
        load_ratio = load / cores if cores else 0.0
        load_level = "ok" if load_ratio < 1.0 else ("warning" if load_ratio < 2.0 else "critical")
        # 内存
        rc, out, _ = _ssh_exec(client, "free -m")
        mem = _parse_free(out)
        mem_level = "ok" if mem and mem["used_pct"] < 85 else ("warning" if mem and mem["used_pct"] < 95 else "critical")
        # 磁盘
        rc, out, _ = _ssh_exec(client, "df -h / | tail -1")
        disk = _parse_df(out)
        disk_level = "ok" if disk and disk["used_pct"] < 85 else ("warning" if disk and disk["used_pct"] < 95 else "critical")
        # Docker 容器
        rc, total, _ = _ssh_exec(client, "docker ps -a -q | wc -l")
        rc2, running, _ = _ssh_exec(client, "docker ps -q | wc -l")
        c_total = int(total.strip() or 0) if rc == 0 else 0
        c_run = int(running.strip() or 0) if rc2 == 0 else 0
        c_level = "ok" if c_total == c_run else "warning"

        metrics = {
            "cores": cores,
            "load": {"value": round(load, 2), "ratio": round(load_ratio, 2), "level": load_level},
            "memory": {**(mem or {}), "level": mem_level},
            "disk": {**(disk or {}), "level": disk_level},
            "containers": {"total": c_total, "running": c_run, "level": c_level},
        }
        res["metrics"] = metrics

        if load_level != "ok":
            res["issues"].append(f"负载 {load:.2f} / {cores} 核")
        if mem_level != "ok":
            res["issues"].append(f"内存使用 {metrics['memory'].get('used_pct', '?')}%")
        if disk_level != "ok":
            res["issues"].append(f"磁盘使用 {metrics['disk'].get('used_pct', '?')}%")
        if c_level != "ok":
            res["issues"].append(f"容器 {c_run}/{c_total} 运行")
        res["ok"] = len(res["issues"]) == 0
    except Exception as e:  # noqa: BLE001
        res["issues"].append(f"SSH 连接失败: {e}")
    finally:
        if client:
            client.close()
    return res


def _parse_free(out: str) -> dict:
    """解析 free -m 输出 -> {total_mb, used_mb, used_pct}（兼容中/英文列名）"""
    for line in (out or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Mem:") or stripped.startswith("内存"):
            parts = stripped.split()
            # 兼容「内存：」冒号粘连
            total = float(parts[1])
            used = float(parts[2])
            if total > 0:
                return {"total_mb": int(total), "used_mb": int(used),
                        "used_pct": round(used / total * 100, 1)}
    return {"total_mb": 0, "used_mb": 0, "used_pct": 0.0}


def _parse_df(out: str) -> dict:
    """解析 df -h 输出 -> {size, used, used_pct}"""
    parts = (out or "").split()
    if len(parts) >= 5:
        try:
            pct = float(parts[4].replace("%", ""))
        except ValueError:
            pct = 0.0
        return {"size": parts[1], "used": parts[2], "used_pct": pct}
    return {"size": "-", "used": "-", "used_pct": 0.0}



def snapshot_metrics():
    """每 60s 采样一次服务器资源 + 设备在线数，写入 metrics_snapshots（趋势图数据源）。"""
    if _app is None:
        return
    with _app.app_context():
        from .extensions import db
        from .models import MetricsSnapshot, Device
        from . import orchestrator
        srv = _collect_server_health()
        m = srv.get("metrics", {})
        online = 0
        try:
            adb = orchestrator.list_adb_devices()
            online = sum(1 for x in adb if x["status"] == "device")
        except Exception:  # noqa: BLE001
            pass
        total = db.session.query(Device).filter_by(backend="redroid").count()
        snap = MetricsSnapshot(
            server_cpu=(m.get("load") or {}).get("ratio", 0) * 100 if (m.get("load") or {}).get("ratio") is not None else None,
            server_mem=(m.get("memory") or {}).get("used_pct"),
            server_disk=(m.get("disk") or {}).get("used_pct"),
            containers=(m.get("containers") or {}).get("running"),
            devices_online=online,
            devices_total=total,
        )
        db.session.add(snap)
        # 只保留最近 7 天数据，防止无限增长
        from datetime import timedelta
        cutoff = _now() - timedelta(days=7)
        db.session.query(MetricsSnapshot).filter(MetricsSnapshot.ts < cutoff).delete()
        db.session.commit()


def billing_check():
    """预付费计费：套餐到期提醒 + 到期断机（每 60s）。

    - 到期前 3 天：提醒一次（expire_soon）；
    - 到期当天：再提醒一次（recharge_hint）；
    - 已到期：置 expired + 断开该用户所有运行中的云手机（expired）。
    幂等：Subscription.reminded 记录已发提醒类型。
    """
    if _app is None:
        return
    with _app.app_context():
        try:
            from . import billing
            billing.run_expiry_reminders()
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            print(f"[billing] 到期检查异常: {e}")
