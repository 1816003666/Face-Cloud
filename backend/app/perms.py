# -*- coding: utf-8 -*-
"""账号级功能授权：平台管理员给「企业管理员」分配能用到什么程度。

定位（2026-10 调整）：**企业管理后台本质就是用户后台，只是多了"部门与角色"。**
所以企业管理员能拿到的上限 = 普通用户的全部能力 + 组织管理，平台侧的
计费管控、退款审核、告警处理、审计、服务器/线路/镜像/诊断/系统设置一律不给。

四个档位（都是企业侧范围）：
- basic        基础：看设备 + 用设备
- standard     标准：+ 创建/删除设备、部门与成员管理
- advanced     高级：+ 导入/轮次、脚本编排与执行
- near_system  全部：+ 文件删除、任务编排（企业侧能拿的全给了）

平台账号不受这套限制（直接放行），所以平台专属能力不需要出现在档位里。
"""
from __future__ import annotations

import json

from .extensions import db
from .models import UserPerm

BASIC = [
    "device.view", "device.control",
    "file.view", "file.upload",
    "plan.view", "sub.view", "order.view",
]

STANDARD = BASIC + [
    "device.create", "device.delete", "device.rename", "device.power",
    "device.group", "device.assign", "device.apk",
    "file.push", "file.browse",
    "org.view", "org.create", "org.member", "org.role",
    "task.view", "bill.stats",
]

ADVANCED = STANDARD + [
    "device.import", "device.rotation",
    "script.view", "script.run", "script.manage",
]

# 企业侧能拿到的全部（= 用户能力 + 组织管理）
NEAR_SYSTEM = ADVANCED + [
    "file.delete",
    "task.run", "task.manage",
]

# 平台专属：企业账号永远拿不到（页面也已对企业隐藏）
PLATFORM_ONLY = frozenset({
    "plan.manage", "refund.audit",          # 计费管控 / 退款审核
    "alert.view", "alert.handle",           # 告警中心（平台运维）
    "audit.view", "audit.export",           # 审计日志
    "server.view", "server.manage",         # 服务器管理
    "line.manage", "vpc.manage",            # 出口线路 / VPC
    "settings.manage", "image.manage", "diag.run",   # 系统设置 / 镜像 / 诊断
})

PRESETS = {
    "basic": {"name": "基础", "desc": "查看并使用分配给自己的云手机", "perms": BASIC},
    "standard": {"name": "标准", "desc": "可创建/删除设备，管理本企业部门与成员", "perms": STANDARD},
    "advanced": {"name": "高级", "desc": "再加设备导入/轮次运行、脚本编排与执行", "perms": ADVANCED},
    "near_system": {"name": "全部功能",
                    "desc": "企业管理员能拿到的全部：用户能力 + 部门与角色",
                    "perms": NEAR_SYSTEM},
}


def ensure_builtin_presets() -> None:
    """把内置四档写进表里（幂等），之后就可以被编辑/新增自定义档位。"""
    from .models import PermPreset
    for i, (code, meta) in enumerate(PRESETS.items()):
        row = db.session.query(PermPreset).filter_by(code=code).first()
        if row is None:
            db.session.add(PermPreset(code=code, name=meta["name"], desc=meta["desc"],
                                      perms=json.dumps(meta["perms"], ensure_ascii=False),
                                      builtin=True, sort=(i + 1) * 10))
        else:
            row.builtin = True
            if not row.sort:
                row.sort = (i + 1) * 10
            # 档位定义调整过（去掉了平台专属能力），内置档位的内容跟着更新
            wanted = json.dumps(meta["perms"], ensure_ascii=False)
            if row.perms != wanted:
                row.perms = wanted
            row.name = meta["name"]
            row.desc = meta["desc"]
    db.session.commit()


def preset_list() -> list:
    """档位清单：优先取数据库（可自定义），表空时回落内置。"""
    from .models import PermPreset
    rows = db.session.query(PermPreset).order_by(PermPreset.sort, PermPreset.id).all()
    if not rows:
        return [{"code": k, "name": v["name"], "desc": v["desc"],
                 "count": len(v["perms"]), "perms": list(v["perms"]),
                 "builtin": True}
                for k, v in PRESETS.items()]
    return [r.to_dict() for r in rows]


def get_preset(code: str):
    from .models import PermPreset
    return db.session.query(PermPreset).filter_by(code=code).first()


def preset_perms(code: str) -> list:
    row = get_preset(code)
    if row is not None:
        try:
            return json.loads(row.perms or "[]")
        except (TypeError, ValueError):
            return []
    meta = PRESETS.get(code)
    return list(meta["perms"]) if meta else []


def get(user_id: int) -> UserPerm | None:
    return db.session.get(UserPerm, user_id)


def perms_of(user_id: int) -> set:
    row = get(user_id)
    if row is None:
        return set()
    try:
        return set(json.loads(row.perms or "[]"))
    except (TypeError, ValueError):
        return set()


def enterprise_scope_perms(perms) -> list:
    """去掉平台专属权限码（企业账号只能用企业侧能力）。"""
    return [c for c in (perms or []) if c not in PLATFORM_ONLY]


def set_perms(user_id: int, perms, preset: str = "") -> UserPerm:
    from .org import ALL_PERMS
    from .models import User
    valid = set(ALL_PERMS)
    owner = db.session.get(User, user_id)
    enterprise_user = owner is not None and owner.enterprise_id is not None
    clean = []
    for code in (perms or []):
        code = str(code or "").strip()
        if code not in valid or code in clean:
            continue
        if enterprise_user and code in PLATFORM_ONLY:
            continue
        clean.append(code)
    row = get(user_id)
    if row is None:
        row = UserPerm(user_id=user_id)
        db.session.add(row)
    row.perms = json.dumps(clean, ensure_ascii=False)
    row.preset = preset or ""
    db.session.commit()
    return row


def sync_builtin_assignments() -> int:
    """把"按内置档位授权"的企业账号同步到最新的档位内容。

    档位定义改过（去掉了平台专属能力）后，以前按旧档位授权的账号要跟着收窄，
    否则存量企业管理员还能调用退款审核之类的接口。
    """
    from .models import PermPreset, User

    changed = 0
    rows = db.session.query(UserPerm).all()
    for row in rows:
        if not row.preset or row.preset not in PRESETS:
            continue
        user = db.session.get(User, row.user_id)
        if user is None or user.enterprise_id is None:
            continue
        codes = PRESETS[row.preset]["perms"]
        new_perms = json.dumps(enterprise_scope_perms(codes), ensure_ascii=False)
        if row.perms != new_perms:
            row.perms = new_perms
            changed += 1
    if changed:
        db.session.commit()
    return changed


def ensure_default_for(user) -> None:
    """企业管理员如果还没配过功能权限，给一份「接近系统级」兜底。"""
    if user is None or getattr(user, "enterprise_id", None) is None:
        return
    if getattr(user, "role", "") not in ("admin", "superadmin"):
        return
    if get(user.id) is not None:
        return
    set_perms(user.id, enterprise_scope_perms(NEAR_SYSTEM), preset="near_system")
