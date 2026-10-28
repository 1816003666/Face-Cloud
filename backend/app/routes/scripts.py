"""脚本管理模块：创建 / 编辑 / 删除 / 执行 / 模板 / 定时任务。

脚本 = 一组操作步骤（steps JSON），可回放到多台设备。
权限：查看登录即可；创建/编辑/删除/执行需 operator 及以上。
"""
import json
import re

from flask import request, jsonify, Blueprint, g
scripts_bp = Blueprint("scripts", __name__)

from ..extensions import db
from ..models import Script, ScriptExecution, Device, DeviceLog, record_audit, raise_alert, iso_utc
from ..auth import login_required, require_role, require_perm

# 内置脚本模板（前端“从模板新建”用）
BUILTIN_TEMPLATES = [
    {"name": "打开指定网址", "steps": [
        {"action": "open_url", "params": {"url": "https://www.example.com"}}]},
    {"name": "安装应用", "steps": [
        {"action": "install", "params": {"pkg": "com.example.app"}}]},
    {"name": "输入文本并回车", "steps": [
        {"action": "text", "params": {"value": "hello"}},
        {"action": "key", "params": {"key": "enter"}}]},
    {"name": "滑动解锁", "steps": [
        {"action": "swipe", "params": {"x1": 500, "y1": 1500, "x2": 500, "y2": 500}}]},
]


@scripts_bp.get("/scripts/templates")
@require_perm("script.view")
def templates():
    return jsonify(BUILTIN_TEMPLATES)


@scripts_bp.get("/scripts/executions")
@require_perm("script.view")
def list_executions():
    """脚本执行历史（可按脚本过滤，默认最近 50 条）。"""
    sid = request.args.get("script_id", type=int)
    q = db.session.query(ScriptExecution)
    if sid is not None:
        q = q.filter(ScriptExecution.script_id == sid)
    from .. import scoping
    if not scoping.is_privileged(g.current_user):
        q = q.filter(ScriptExecution.executed_by == g.current_user.id)
    rows = q.order_by(ScriptExecution.id.desc()).limit(50).all()
    return jsonify([_ser_exec(e) for e in rows])


def _ser_exec(e: ScriptExecution):
    try:
        detail = json.loads(e.detail) if e.detail else []
    except Exception:  # noqa: BLE001
        detail = []
    return {
        "id": e.id,
        "script_id": e.script_id,
        "script_name": e.script_name,
        "status": e.status,
        "total": e.total,
        "ok": e.ok,
        "failed": e.failed,
        "detail": detail,
        "executed_by": e.executed_by,
        "created_at": iso_utc(e.created_at),
    }


@scripts_bp.get("/scripts")
@require_perm("script.view")
def list_scripts():
    rows = db.session.query(Script).order_by(Script.id.desc()).all()
    return jsonify([_serialize(s) for s in rows])


@scripts_bp.post("/scripts")
@require_perm("script.manage")
def create_script():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    steps = data.get("steps", [])
    if not name:
        return jsonify(error="脚本名必填"), 400
    if not isinstance(steps, list):
        return jsonify(error="steps 必须是数组"), 400
    variables = data.get("variables") or {}
    if not isinstance(variables, dict):
        return jsonify(error="variables 必须是对象"), 400
    s = Script(name=name, steps=json.dumps(steps, ensure_ascii=False),
               variables=json.dumps(variables, ensure_ascii=False),
               owner_id=g.current_user.id)
    db.session.add(s)
    db.session.commit()
    record_audit(g.current_user.id, "create_script", "script", s.id,
                {"name": name, "steps": len(steps)})
    return jsonify(_serialize(s)), 201


@scripts_bp.get("/scripts/<int:sid>")
@require_perm("script.view")
def get_script(sid):
    s = db.session.get(Script, sid)
    if s is None:
        return jsonify(error="脚本不存在"), 404
    return jsonify(_serialize(s))


@scripts_bp.patch("/scripts/<int:sid>")
@require_perm("script.manage")
def update_script(sid):
    s = db.session.get(Script, sid)
    if s is None:
        return jsonify(error="脚本不存在"), 404
    data = request.get_json(silent=True) or {}
    if "name" in data:
        s.name = data["name"]
    if "steps" in data and isinstance(data["steps"], list):
        s.steps = json.dumps(data["steps"], ensure_ascii=False)
    if "variables" in data and isinstance(data["variables"], dict):
        s.variables = json.dumps(data["variables"], ensure_ascii=False)
    db.session.commit()
    record_audit(g.current_user.id, "update_script", "script", sid,
                {"name": s.name})
    return jsonify(_serialize(s))


@scripts_bp.delete("/scripts/<int:sid>")
@require_perm("script.manage")
def delete_script(sid):
    s = db.session.get(Script, sid)
    if s is None:
        return jsonify(error="脚本不存在"), 404
    db.session.delete(s)
    db.session.commit()
    record_audit(g.current_user.id, "delete_script", "script", sid,
                {"name": s.name})
    return jsonify(ok=True)


@scripts_bp.get("/scripts/<int:sid>/export")
@require_perm("script.view")
def export_script(sid):
    """导出单个脚本为 JSON（可离线编辑后再导入）。"""
    from .. import scoping
    s = db.session.get(Script, sid)
    if s is None:
        return jsonify(error="脚本不存在"), 404
    if not scoping.is_privileged(g.current_user) and s.owner_id != g.current_user.id:
        return jsonify(error="无权导出该脚本"), 403
    try:
        steps = json.loads(s.steps or "[]")
    except (TypeError, ValueError):
        steps = []
    payload = {"format": "cloud-phone-script/1", "name": s.name, "steps": steps,
               "variables": _script_variables(s)}
    return jsonify(payload)


@scripts_bp.post("/scripts/import")
@require_perm("script.manage")
def import_script():
    """导入脚本 JSON（format=cloud-phone-script/1），重名自动加后缀。"""
    data = request.get_json(silent=True) or {}
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        return jsonify(error="步骤列表为空，无法导入"), 400
    name = (data.get("name") or "导入的脚本").strip()[:100] or "导入的脚本"
    # 重名加后缀，避免覆盖
    base = name
    n = 2
    while db.session.query(Script).filter(Script.name == name,
                                          Script.owner_id == g.current_user.id).first():
        name = f"{base} ({n})"
        n += 1
    variables = data.get("variables") or {}
    if not isinstance(variables, dict):
        variables = {}
    s = Script(name=name, steps=json.dumps(steps, ensure_ascii=False),
               variables=json.dumps(variables, ensure_ascii=False),
               owner_id=g.current_user.id)
    db.session.add(s)
    db.session.commit()
    record_audit(g.current_user.id, "import_script", "script", s.id,
                 {"name": name, "steps": len(steps)})
    return jsonify(id=s.id, name=name, steps=steps), 201


@scripts_bp.post("/scripts/<int:sid>/duplicate")
@require_perm("script.manage")
def duplicate_script(sid):
    """复制一个脚本（新脚本名为「原名 - 副本」）。"""
    s = db.session.get(Script, sid)
    if s is None:
        return jsonify(error="脚本不存在"), 404
    new = Script(
        name=f"{s.name} - 副本",
        steps=s.steps,
        owner_id=g.current_user.id,
    )
    db.session.add(new)
    db.session.commit()
    record_audit(g.current_user.id, "duplicate_script", "script", new.id,
                {"from": sid, "name": new.name})
    return jsonify(_serialize(new)), 201


@scripts_bp.post("/scripts/<int:sid>/execute")
@require_perm("script.run")
def execute_script(sid):
    """在指定设备上真实回放脚本（redroid 走 ADB 操控），并记录执行历史与明细。

    脚本步骤 action 支持：open_url / tap / swipe / text / key / wait / sequence / install。
    """
    from .. import orchestrator

    s = db.session.get(Script, sid)
    if s is None:
        return jsonify(error="脚本不存在"), 404
    body = request.get_json(silent=True) or {}
    device_ids = body.get("device_ids", [])
    if not device_ids:
        return jsonify(error="请选择至少一台设备"), 400
    from .. import scoping
    _bad = scoping.foreign_device_ids(g.current_user, device_ids)
    if _bad:
        return jsonify(error="无权操作部分设备", invalid_ids=_bad), 403

    steps = json.loads(s.steps)
    # 变量：脚本默认值 ← 本次执行传入的覆盖值；再替换掉步骤里的 ${name}
    run_vars = _script_variables(s)
    passed = body.get("variables") or {}
    if isinstance(passed, dict):
        run_vars.update({str(k): v for k, v in passed.items()})
    missing = [n for n in _placeholders(steps) if n not in run_vars]
    if missing:
        return jsonify(error=f"缺少变量值：{'、'.join(missing)}", missing=missing), 400
    steps = _render(steps, run_vars)
    ok = 0
    results = []
    failed_ids = []
    for did in device_ids:
        dev = db.session.get(Device, int(did))
        if dev is None:
            failed_ids.append(did)
            results.append({"device_id": int(did), "device_name": "?", "serial": "",
                            "ok": False, "message": "设备不存在"})
            continue
        ok_steps, fail_steps = 0, 0
        fail_msgs = []
        for idx, step in enumerate(steps, start=1):
            try:
                result = _run_script_step(dev, step)
                if result.get("ok"):
                    ok_steps += 1
                else:
                    fail_steps += 1
                    fail_msgs.append(f"步骤{idx}: {result.get('result', '执行失败')}")
            except Exception as e:  # noqa: BLE001
                fail_steps += 1
                fail_msgs.append(f"步骤{idx}: {e}")
        device_ok = fail_steps == 0
        if device_ok:
            ok += 1
        else:
            failed_ids.append(did)
        summary = (f"完成 {ok_steps}/{len(steps)} 步" if device_ok
                   else f"失败 {fail_steps} 步：" + "；".join(fail_msgs[:3]))
        db.session.add(DeviceLog(
            device_id=dev.id, level="info",
            message=f"执行脚本[{s.name}] {summary}"))
        results.append({"device_id": dev.id, "device_name": dev.name,
                        "serial": dev.serial, "ok": device_ok, "message": summary})

    failed = len(device_ids) - ok
    status = "success" if failed == 0 else ("partial" if ok else "failed")
    ex = ScriptExecution(
        script_id=s.id, script_name=s.name, status=status,
        total=len(device_ids), ok=ok, failed=failed,
        detail=json.dumps(results, ensure_ascii=False),
        executed_by=g.current_user.id,
    )
    db.session.add(ex)
    db.session.commit()
    if failed > 0:
        raise_alert("operation_failure", "warning",
                    f"脚本[{s.name}] 执行失败 {failed} 台设备",
                    detail={"script": s.name, "failed_device_ids": failed_ids})
    record_audit(g.current_user.id, "execute_script", "script", sid,
                {"name": s.name, "devices": device_ids, "execution_id": ex.id})
    return jsonify(ok=ok, failed=failed, execution_id=ex.id, results=results)


# 常用按键名 → Android keycode
_KEYCODES = {
    "home": 3, "back": 4, "enter": 66, "menu": 82,
    "volume_up": 24, "volume_down": 25, "power": 26,
    "app_switch": 187, "escape": 111, "del": 67, "tab": 61,
    "space": 62, "search": 84, "camera": 27, "settings": 176,
}


def _run_script_step(dev: Device, step: dict) -> dict:
    """把脚本的一个步骤翻译成 orchestrator 真实操控。"""
    from .. import orchestrator
    action = step.get("action", "")
    p = step.get("params") or {}
    action_map = {
        "open_url": {"url": p.get("url", "https://www.baidu.com")},
        "tap": {"x": p.get("x", 500), "y": p.get("y", 500)},
        "swipe": {"x1": p.get("x1", 100), "y1": p.get("y1", 500),
                  "x2": p.get("x2", 400), "y2": p.get("y2", 500),
                  "duration": p.get("duration", 300)},
        "text": {"text": p.get("value", p.get("text", ""))},
        "wait": {"seconds": p.get("seconds", 1)},
    }
    if action == "key":
        key = p.get("key", p.get("keycode", "home"))
        if isinstance(key, str) and key.lower() in _KEYCODES:
            key = _KEYCODES[key.lower()]
        key = int(key)
        return orchestrator.control_device(dev.backend, dev.serial, "key", {"keycode": key})
    if action == "install":
        pkg = p.get("pkg", "")
        if not pkg:
            return {"ok": False, "result": "install 需要 pkg 参数"}
        if dev.backend == "simulator":
            return {"ok": True, "result": f"[simulator] launch {pkg}"}
        orchestrator.adb_connect(dev.serial)
        rc, out, err = orchestrator._adb_shell(dev.serial, f"monkey -p {pkg} 1")
        return {"ok": rc == 0, "result": f"打开应用 {pkg}"}
    if action in action_map:
        return orchestrator.control_device(dev.backend, dev.serial, action, action_map[action])
    if action == "sequence":
        inner = p.get("steps") or []
        ok_all = True
        for st in inner:
            r = _run_script_step(dev, st)
            if not r.get("ok"):
                ok_all = False
                break
        return {"ok": ok_all, "result": f"sequence {len(inner)} 步"}
    return {"ok": False, "result": f"未知动作 {action}"}


_VAR_RE = re.compile(r"\$\{([^}]{1,32})\}")


def _placeholders(steps) -> list:
    """从步骤里抽出用到的 ${变量名}（保序去重）。"""
    names = []
    for step in steps or []:
        blob = json.dumps(step or {}, ensure_ascii=False)
        for m in _VAR_RE.finditer(blob):
            n = m.group(1).strip()
            if n and n not in names:
                names.append(n)
    return names


def _render(obj, variables: dict):
    """把 ${name} 替换成变量值（递归处理 dict/list/str）。没给值的保留原样。"""
    if isinstance(obj, dict):
        return {k: _render(v, variables) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_render(v, variables) for v in obj]
    if isinstance(obj, str):
        def _sub(m):
            name = m.group(1).strip()
            return str(variables[name]) if name in variables else m.group(0)
        return _VAR_RE.sub(_sub, obj)
    return obj


def _script_variables(s) -> dict:
    try:
        v = json.loads(s.variables or "{}")
    except (TypeError, ValueError):
        v = {}
    return v if isinstance(v, dict) else {}


def _serialize(s: Script):
    return {
        "variables": _script_variables(s),
        "variable_names": _placeholders(json.loads(s.steps or "[]")),
        "id": s.id,
        "name": s.name,
        "steps": json.loads(s.steps) if s.steps else [],
        "owner_id": s.owner_id,
        "created_at": iso_utc(s.created_at),
    }
