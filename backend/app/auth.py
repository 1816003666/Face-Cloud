"""认证与权限核心。

- 密码：pbkdf2_hmac(sha256, 20万轮) + 随机 salt，存 pbkdf2_sha256$轮数$salt$dk
- JWT：HS256，payload={sub:用户id, exp}；从 app.config["JWT_SECRET"] 取密钥
- 依赖：login_required / require_role(min_role) 装饰器

设计要点（面试/作业加分项）：
1. 校验密码用 hmac.compare_digest 防时序攻击；
2. 角色分级 viewer<operator<admin<superadmin，require_role 做最小权限拦截；
3. 解析失败/过期一律 401，权限不足 403，绝不静默放行。
"""
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from flask import request, g, jsonify

from .extensions import db
from . import tenant
from .models import User, ROLE_LEVELS


# ----------------------------- 密码哈希 -----------------------------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"pbkdf2_sha256$200000${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, dk_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt_hex), int(rounds),
        )
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def validate_password(password: str):
    """密码复杂度校验：≥6 位且含字母和数字。返回 (ok:bool, msg:str)。"""
    if not password or len(password) < 6:
        return False, "密码至少 6 位"
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return False, "密码须同时包含字母和数字"
    return True, ""


# ----------------------------- JWT -----------------------------
def create_access_token(user_id: int) -> str:
    from flask import current_app
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc)
        + timedelta(hours=current_app.config["JWT_EXPIRE_HOURS"]),
    }
    return jwt.encode(payload, current_app.config["JWT_SECRET"], algorithm="HS256")


def _resolve_user():
    """从 Authorization: Bearer <token> 解析出当前用户，失败返回 None。"""
    from flask import current_app
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    try:
        payload = jwt.decode(
            token, current_app.config["JWT_SECRET"], algorithms=["HS256"]
        )
        return db.session.get(User, int(payload["sub"]))
    except Exception:
        return None


# ----------------------------- 装饰器 -----------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = _resolve_user()
        if user is None or not user.is_active:
            return jsonify(error="登录已失效，请重新登录"), 401
        tenant.set_current(user)
        return f(*args, **kwargs)
    return wrapper


def require_role(min_role: str):
    """角色不低于 min_role 才可访问；admin 不能越过 superadmin。

    **企业账号不算平台管理员**：企业管理员（role=admin 但 enterprise_id 非空）
    的平台角色只是"本企业管理权限"的代称，不能用来调平台运维接口。
    以前这里只比 role 等级，导致企业管理员能访问 /api/users 等平台接口。
    """
    min_lv = ROLE_LEVELS[min_role]

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = _resolve_user()
            if user is None or not user.is_active:
                return jsonify(error="登录已失效，请重新登录"), 401
            ent = getattr(user, "enterprise_id", None)
            level = ROLE_LEVELS.get(user.role, 0)
            if ent is not None and min_lv >= ROLE_LEVELS["admin"]:
                return jsonify(error="仅平台账号可访问"), 403
            if level < min_lv:
                return jsonify(error="权限不足"), 403
            tenant.set_current(user)
            return f(*args, **kwargs)
        return wrapper
    return decorator


def require_platform(f):
    """仅平台账号（无企业归属的 admin / superadmin）可访问。"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = _resolve_user()
        if user is None or not user.is_active:
            return jsonify(error="登录已失效，请重新登录"), 401
        ent = getattr(user, "enterprise_id", None)
        if ent is not None or user.role not in ("admin", "superadmin"):
            return jsonify(error="仅平台账号可访问"), 403
        tenant.set_current(user)
        return f(*args, **kwargs)
    return wrapper


def has_perm(user, code: str) -> bool:
    """判断账号是否具备某个功能权限（见 app/org.py 的 MODULES）。

    - **平台账号**（无企业归属的 admin / superadmin）直接放行：跨企业管理；
    - 企业管理员/成员：按「组织角色权限 + 账号级授权」的并集判断，
      企业管理员能用多少由平台管理员在「用户管理 → 功能权限」里分配。
    """
    if user is None or not user.is_active:
        return False
    ent = getattr(user, "enterprise_id", None)
    if ent is None and user.role in ("admin", "superadmin"):
        return True
    from . import org
    # 基础功能（设备/文件/脚本/任务/套餐）所有登录用户都有；
    # 企业角色在此基础上追加（导入、编排、退款审核等管理类权限）。
    if code in org.BASIC_PERMS:
        return True
    return code in org.user_perms(user.id)


def require_perm(code: str):
    """按功能权限码鉴权：缺少该权限返回 403。"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = _resolve_user()
            if user is None or not user.is_active:
                return jsonify(error="登录已失效，请重新登录"), 401
            if not has_perm(user, code):
                return jsonify(error=f"权限不足（缺少 {code}）", code=code), 403
            tenant.set_current(user)
            return f(*args, **kwargs)
        return wrapper
    return decorator
