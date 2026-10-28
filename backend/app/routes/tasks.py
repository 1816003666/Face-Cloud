"""定时任务路由：定义 / 列表 / 立即执行，调度由 scheduler.py 后台驱动。"""
import json
from datetime import datetime, timedelta
from flask import request, jsonify, g, Blueprint
tasks_bp = Blueprint("tasks", __name__)
from ..extensions import db
from ..models import (ScheduledTask, TaskExecution, TASK_ACTION, SCHEDULE_TYPE,
                     PRIVILEGED_TASK_ACTIONS, record_audit, iso_utc)
from ..auth import login_required, require_role, require_perm


@tasks_bp.get("/tasks")
@require_perm("task.view")
def list_tasks():
    from .. import scoping
    me = g.current_user
    q = db.session.query(ScheduledTask)
    if scoping.is_platform_admin(me):
        pass                      # 平台管理员：跨企业运维视图
    else:
        # 任务数据不共享：除了平台管理员，谁建的谁看（企业管理员也不看下属的任务）
        q = q.filter(ScheduledTask.created_by == me.id)
        q = q.filter(~ScheduledTask.action.in_(PRIVILEGED_TASK_ACTIONS))
    tasks = q.order_by(ScheduledTask.id).all()
    out = [_task_to_dict(t) for t in tasks]
    _attach_last_run(out)
    return jsonify(out)


def _attach_last_run(rows: list) -> list:
    """给任务列表补「最近一次执行」结果，界面上不用点历史也能看出上次跑成没跑。"""
    ids = [r["id"] for r in rows]
    if not ids:
        return rows
    latest = {}
    for e in (db.session.query(TaskExecution)
              .filter(TaskExecution.task_id.in_(ids))
              .order_by(TaskExecution.id.desc()).limit(len(ids) * 8).all()):
        if e.task_id not in latest:
            latest[e.task_id] = e
    for r in rows:
        e = latest.get(r["id"])
        if e is None:
            r["last_run"] = None
            continue
        r["last_run"] = {
            "status": e.status,
            "at": e.finished_at.isoformat() if e.finished_at else (e.started_at.isoformat() if e.started_at else None),
            "ok": e.ok or 0,
            "failed": e.failed or 0,
            "total": e.total or 0,
        }
    return rows


@tasks_bp.get("/tasks/<int:tid>/executions")
@require_perm("task.view")
def list_task_executions(tid):
    """某任务的执行历史（含 health_check 巡检报告 detail）。"""
    from .. import scoping
    t = db.session.get(ScheduledTask, tid)
    if t is None:
        return jsonify(error="任务不存在"), 404
    if not _can_touch(g.current_user, t):
        return jsonify(error="无权访问该任务"), 403
    # 巡检报告的 detail 含全平台设备明细，普通用户即使是自己的历史记录也不给看
    if t.action in PRIVILEGED_TASK_ACTIONS and not scoping.is_privileged(g.current_user):
        return jsonify(error="健康巡检报告仅管理员可查看"), 403
    rows = db.session.query(TaskExecution).filter(
        TaskExecution.task_id == tid).order_by(TaskExecution.id.desc()).limit(30).all()
    return jsonify([_exec_to_dict(e) for e in rows])


def _can_touch(user, task) -> bool:
    """能不能看/改/跑这条任务。

    规则：平台管理员可以操作全部；其他人只能操作自己创建的任务。
    （任务数据不共享，企业管理员也看不到成员的任务。）
    """
    from .. import scoping
    if task is None or user is None:
        return False
    if scoping.is_platform_admin(user):
        return True
    return task.created_by == user.id


def _exec_to_dict(e: TaskExecution) -> dict:
    try:
        detail = json.loads(e.detail) if e.detail else {}
    except Exception:  # noqa: BLE001
        detail = {}
    return {
        "id": e.id,
        "task_id": e.task_id,
        "status": e.status,
        "started_at": iso_utc(e.started_at),
        "finished_at": iso_utc(e.finished_at),
        "total": e.total,
        "ok": e.ok,
        "failed": e.failed,
        "detail": detail,
    }


@tasks_bp.post("/tasks")
@require_perm("task.manage")
def create_task():
    cur = g.current_user
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    action = data.get("action")
    if not name:
        return jsonify(error="任务名必填"), 400
    if action not in TASK_ACTION:
        return jsonify(error="非法动作"), 400
    from .. import scoping as _scoping
    if action in PRIVILEGED_TASK_ACTIONS and not _scoping.is_privileged(cur):
        return jsonify(error="健康巡检为平台运维功能，仅管理员可用"), 403

    # —— 参数校验：不给参数就建任务，执行时必然"0 台设备、0 个成功"的假成功 ——
    params = data.get("params") or {}
    device_ids = data.get("device_ids") or []
    if not isinstance(params, dict):
        return jsonify(error="params 必须是对象"), 400
    if action != "health_check" and not device_ids:
        return jsonify(error="请选择要执行的云手机"), 400
    if action == "install" and not params.get("file_id"):
        return jsonify(error="安装应用需要先选择 APK 文件"), 400
    if action == "open_url" and not (params.get("url") or "").strip():
        return jsonify(error="打开网址需要填写 URL"), 400
    if action == "sequence" and not params.get("steps"):
        return jsonify(error="序列任务至少需要一个步骤"), 400
    if action == "run_script" and not params.get("script_id"):
        return jsonify(error="执行脚本任务需要先选择脚本"), 400
    if action == "run_script" and not params.get("script_id"):
        return jsonify(error="执行脚本任务需要先选择脚本"), 400

    schedule_type = data.get("schedule_type", "once")
    cron_expr = (data.get("cron_expr") or "").strip()
    if schedule_type not in SCHEDULE_TYPE:
        return jsonify(error="非法调度类型"), 400
    if schedule_type == "cron":
        try:
            from croniter import croniter
            croniter(cron_expr, datetime.utcnow())
        except Exception:
            return jsonify(error="非法 cron 表达式"), 400
    _bad = _scoping.foreign_device_ids(cur, device_ids)
    if _bad:
        return jsonify(error="无权操作部分设备", invalid_ids=_bad), 403
    t = ScheduledTask(
        name=name,
        action=action,
        params=json.dumps(params, ensure_ascii=False),
        device_ids=json.dumps(device_ids, ensure_ascii=False),
        schedule_type=schedule_type,
        interval_seconds=data.get("interval_seconds", 3600),
        cron_expr=cron_expr,
        next_run=_compute_next(schedule_type,
                               data.get("interval_seconds", 3600),
                               cron_expr),
        enabled=bool(data.get("enabled", True)),
        max_retries=max(0, int(data.get("max_retries") or 0)),
        created_by=cur.id,
    )
    db.session.add(t)
    db.session.commit()
    record_audit(cur.id, "create_task", "task", t.id, {"name": name, "action": action})
    return jsonify(_task_to_dict(t)), 201


@tasks_bp.post("/tasks/<int:tid>/executions/<int:eid>/retry")
@require_perm("task.run")
def retry_failed_devices(tid, eid):
    """只重跑某次执行里失败的设备（成功的那些不再动）。"""
    from .. import scoping
    cur = g.current_user
    t = db.session.get(ScheduledTask, tid)
    if t is None:
        return jsonify(error="任务不存在"), 404
    if not _can_touch(cur, t):
        return jsonify(error="无权执行该任务"), 403
    if t.action in PRIVILEGED_TASK_ACTIONS and not scoping.is_privileged(cur):
        return jsonify(error="健康巡检为平台运维功能，仅管理员可用"), 403

    e = db.session.get(TaskExecution, eid)
    if e is None or e.task_id != tid:
        return jsonify(error="执行记录不存在"), 404
    try:
        detail = json.loads(e.detail or "[]")
    except (TypeError, ValueError):
        detail = []
    failed_ids = []
    for item in detail if isinstance(detail, list) else []:
        if isinstance(item, dict) and item.get("ok") is False and item.get("device_id"):
            failed_ids.append(int(item["device_id"]))
    if not failed_ids:
        return jsonify(error="这次执行没有失败的设备"), 400
    bad = scoping.foreign_device_ids(cur, failed_ids)
    if bad:
        return jsonify(error="无权操作部分设备", invalid_ids=bad), 403

    from ..scheduler import execute_task
    exec_id = execute_task(t, actor_id=cur.id, device_ids_override=failed_ids)
    record_audit(cur.id, "retry_task_devices", "task", t.id,
                 {"from_execution": eid, "device_ids": failed_ids})
    return jsonify(execution_id=exec_id, device_ids=failed_ids)


@tasks_bp.put("/tasks/<int:tid>")
@require_perm("task.manage")
def update_task(tid):
    """编辑任务：名称 / 动作 / 参数 / 设备 / 调度方式都能改。"""
    from .. import scoping
    cur = g.current_user
    t = db.session.get(ScheduledTask, tid)
    if t is None:
        return jsonify(error="任务不存在"), 404
    if not _can_touch(cur, t):
        return jsonify(error="无权修改该任务"), 403
    if t.action in PRIVILEGED_TASK_ACTIONS and not scoping.is_privileged(cur):
        return jsonify(error="健康巡检为平台运维功能，仅管理员可用"), 403

    data = request.get_json(silent=True) or {}
    action = data.get("action", t.action)
    if action not in TASK_ACTION:
        return jsonify(error="非法动作"), 400
    if action in PRIVILEGED_TASK_ACTIONS and not scoping.is_privileged(cur):
        return jsonify(error="健康巡检为平台运维功能，仅管理员可用"), 403

    params = data.get("params")
    if params is not None and not isinstance(params, dict):
        return jsonify(error="params 必须是对象"), 400
    params = params if params is not None else json.loads(t.params or "{}")
    device_ids = data.get("device_ids")
    device_ids = device_ids if device_ids is not None else json.loads(t.device_ids or "[]")

    if action != "health_check" and not device_ids:
        return jsonify(error="请选择要执行的云手机"), 400
    if action == "install" and not params.get("file_id"):
        return jsonify(error="安装应用需要先选择 APK 文件"), 400
    if action == "open_url" and not (params.get("url") or "").strip():
        return jsonify(error="打开网址需要填写 URL"), 400
    if action == "sequence" and not params.get("steps"):
        return jsonify(error="序列任务至少需要一个步骤"), 400

    bad = scoping.foreign_device_ids(cur, device_ids)
    if bad:
        return jsonify(error="无权操作部分设备", invalid_ids=bad), 403

    name = (data.get("name") or t.name or "").strip()
    if not name:
        return jsonify(error="任务名必填"), 400
    schedule_type = data.get("schedule_type", t.schedule_type)
    if schedule_type not in SCHEDULE_TYPE:
        return jsonify(error="非法调度类型"), 400
    cron_expr = (data.get("cron_expr") if data.get("cron_expr") is not None
                 else (t.cron_expr or "")).strip()
    if schedule_type == "cron":
        try:
            from croniter import croniter
            croniter(cron_expr, datetime.utcnow())
        except Exception:  # noqa: BLE001
            return jsonify(error="非法 cron 表达式"), 400

    t.name = name
    t.action = action
    t.params = json.dumps(params, ensure_ascii=False)
    t.device_ids = json.dumps(device_ids, ensure_ascii=False)
    t.schedule_type = schedule_type
    t.interval_seconds = int(data.get("interval_seconds", t.interval_seconds or 3600))
    t.cron_expr = cron_expr
    t.enabled = bool(data.get("enabled", t.enabled))
    try:
        t.max_retries = max(0, int(data.get("max_retries") if data.get("max_retries") is not None
                                   else (t.max_retries or 0)))
    except (TypeError, ValueError):
        return jsonify(error="重试次数不合法"), 400
    t.next_run = _compute_next(schedule_type, t.interval_seconds, cron_expr)
    db.session.commit()
    record_audit(cur.id, "update_task", "task", t.id, {"name": name, "action": action})
    out = _task_to_dict(t)
    _attach_last_run([out])
    return jsonify(out)


@tasks_bp.patch("/tasks/<int:tid>/enabled")
@require_perm("task.manage")
def toggle_task(tid):
    """启用 / 停用任务（停用后调度器不再执行，但仍保留配置与历史）。"""
    from .. import scoping
    cur = g.current_user
    t = db.session.get(ScheduledTask, tid)
    if t is None:
        return jsonify(error="任务不存在"), 404
    if not _can_touch(cur, t):
        return jsonify(error="无权修改该任务"), 403
    if t.action in PRIVILEGED_TASK_ACTIONS and not scoping.is_privileged(cur):
        return jsonify(error="健康巡检为平台运维功能，仅管理员可用"), 403
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", not t.enabled))
    t.enabled = enabled
    if enabled:
        t.next_run = _compute_next(t.schedule_type, t.interval_seconds or 3600, t.cron_expr or "")
    db.session.commit()
    record_audit(cur.id, "toggle_task", "task", t.id, {"enabled": enabled})
    return jsonify(_task_to_dict(t))


@tasks_bp.post("/tasks/<int:tid>/run")
@require_perm("task.run")
def run_task(tid):
    cur = g.current_user
    t = db.session.get(ScheduledTask, tid)
    if t is None:
        return jsonify(error="任务不存在"), 404
    from .. import scoping
    # 原来完全没有归属校验：普通用户可以执行任意人（含管理员）的任务
    if not _can_touch(cur, t):
        return jsonify(error="无权执行该任务"), 403
    if t.action in PRIVILEGED_TASK_ACTIONS and not scoping.is_privileged(cur):
        return jsonify(error="健康巡检为平台运维功能，仅管理员可用"), 403
    from ..scheduler import execute_task
    exec_id = execute_task(t, actor_id=cur.id)
    return jsonify(execution_id=exec_id)


@tasks_bp.delete("/tasks/<int:tid>")
@require_perm("task.manage")
def delete_task(tid):
    cur = g.current_user
    t = db.session.get(ScheduledTask, tid)
    if t is None:
        return jsonify(error="任务不存在"), 404
    from .. import scoping
    if not _can_touch(cur, t):
        return jsonify(error="无权删除该任务"), 403
    db.session.delete(t)
    db.session.commit()
    record_audit(cur.id, "delete_task", "task", tid, {"name": t.name})
    return jsonify(message="已删除")


def _compute_next(schedule_type, interval_seconds, cron_expr=""):
    if schedule_type == "once":
        return datetime.utcnow()
    if schedule_type == "cron":
        from croniter import croniter
        return croniter(cron_expr, datetime.utcnow()).get_next(datetime)
    return datetime.utcnow() + timedelta(seconds=interval_seconds)


def _task_to_dict(t: ScheduledTask) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "action": t.action,
        "params": json.loads(t.params) if t.params else {},
        "device_ids": json.loads(t.device_ids) if t.device_ids else [],
        "schedule_type": t.schedule_type,
        "interval_seconds": t.interval_seconds,
        "cron_expr": t.cron_expr or "",
        "next_run": t.next_run.isoformat() if t.next_run else None,
        "enabled": t.enabled,
        "max_retries": t.max_retries or 0,
        "created_by": t.created_by,
    }
