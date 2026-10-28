"""鉴权路由：登录、当前用户、登出（无状态，登出由前端清 token）。

登录安全（增强）：
- 连续失败 >=5 次锁定账号 15 分钟；
- 连续失败 >=3 次起要求算术验证码，防暴力撞库。
"""
import random
import uuid
from datetime import timedelta
from flask import request, jsonify, current_app, Blueprint
auth_bp = Blueprint("auth", __name__)
from ..extensions import db
from ..models import User, record_audit, _utcnow
from ..auth import (hash_password, verify_password, create_access_token,
                    _resolve_user, validate_password, ROLE_LEVELS)

# 单进程内存验证码存储：captcha_id -> answer（简单算数题）
_captcha_store = {}
MAX_FAILS = 5
CAPTCHA_AFTER = 3
LOCK_MINUTES = 15

# ---- 找回密码：内存验证码存储（生产应改为短信/邮件通道下发）----
_reset_store = {}            # username -> {code, expires_at, attempts, last_sent_at}
RESET_CODE_TTL = timedelta(minutes=10)
RESET_SEND_COOLDOWN = timedelta(seconds=60)
RESET_MAX_ATTEMPTS = 5


def _gen_captcha():
    a = random.randint(2, 9)
    b = random.randint(2, 9)
    cid = uuid.uuid4().hex[:12]
    _captcha_store[cid] = str(a + b)
    return {"captcha_id": cid, "question": f"{a} + {b} = ?"}


def _enterprise_of(user):
    """账号所属企业；平台账号返回 None。"""
    from ..models import Enterprise
    ent_id = getattr(user, "enterprise_id", None)
    if not ent_id:
        return None
    return db.session.get(Enterprise, ent_id)


def _user_payload(user):
    """用户信息 + 企业信息 + 有效功能权限（前端据此渲染菜单和按钮）。"""
    from .. import org as org_mod

    data = user.to_dict()
    ent = _enterprise_of(user)
    data["enterprise"] = ({"id": ent.id, "name": ent.name, "code": ent.code,
                           "status": ent.status or "active"} if ent is not None else None)
    # 平台账号 = 无企业归属 + 管理员角色；无企业的普通账号是"个人用户"
    data["is_platform"] = ent is None and user.role in ("admin", "superadmin")

    # 平台账号（无企业）直通全部功能；企业账号取"账号级授权 + 组织角色"的并集
    if ent is None and user.role in ("admin", "superadmin"):
        data["perms"] = list(org_mod.ALL_PERMS)
    else:
        # 基础功能（个人/企业用户都有）+ 企业角色追加
        data["perms"] = sorted(org_mod.effective_perms(user))
    return data


def _find_enterprise(code):
    """按企业编码找企业（企业版登录页用）。"""
    from ..models import Enterprise
    code = (code or "").strip()
    if not code:
        return None
    return (db.session.query(Enterprise)
            .filter(db.func.lower(Enterprise.code) == code.lower())
            .first())


@auth_bp.get("/auth/enterprise/<code>")
def enterprise_public(code):
    """企业版登录页：按编码查企业（公开接口，只回名称/状态，不泄露其它信息）。"""
    ent = _find_enterprise(code)
    if ent is None:
        return jsonify(error="企业编码不存在"), 404
    return jsonify(id=ent.id, name=ent.name, code=ent.code,
                   status=ent.status or "active")


@auth_bp.post("/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username or not password:
        return jsonify(error="用户名和密码必填"), 400

    # 企业版入口：必须填企业编码，且账号必须属于该企业
    ent_code = (data.get("enterprise_code") or "").strip()
    login_ent = None
    if ent_code:
        login_ent = _find_enterprise(ent_code)
        if login_ent is None:
            return jsonify(error="企业编码不存在，请向管理员确认"), 404
        if (login_ent.status or "active") != "active":
            return jsonify(error="该企业已停用，请联系平台管理员"), 403

    now = _utcnow()
    user = db.session.query(User).filter_by(username=username).first()

    # 影子账号（门户用户）与管理员的行为不同：
    # - 影子账号：本地没有可用密码，直接走门户；绝不累计本地失败/锁定
    # - 管理员：只认本地密码，绝不回落门户（防提权）
    shadow_hash = str(current_app.config.get("PORTAL_SHADOW_PASSWORD_HASH") or "")
    is_shadow = bool(user) and bool(shadow_hash) and (user.hashed_password == shadow_hash)
    is_admin = bool(user) and user.role in ("admin", "superadmin")

    if not is_shadow:
        # 1) 锁定检查
        if user is not None and user.locked_until and user.locked_until > now:
            remain = int((user.locked_until - now).total_seconds() // 60) + 1
            return jsonify(error=f"账号已锁定，请 {remain} 分钟后重试"), 423

        # 2) 连续失败达阈值要求验证码
        need_captcha = user is not None and (user.failed_attempts or 0) >= CAPTCHA_AFTER
        if need_captcha:
            cid = data.get("captcha_id")
            ans = data.get("captcha")
            bad = True
            if cid and _captcha_store.get(cid) is not None:
                if str(_captcha_store.pop(cid, None)) == str(ans or "").strip():
                    bad = False
            if bad:
                user.failed_attempts = (user.failed_attempts or 0) + 1
                if user.failed_attempts >= MAX_FAILS:
                    user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                    user.failed_attempts = 0
                    db.session.commit()
                    return jsonify(error=f"连续失败次数过多，账号已锁定 {LOCK_MINUTES} 分钟"), 423
                db.session.commit()
                return jsonify(error="验证码错误" if cid else "请填写验证码",
                               need_captcha=True, captcha=_gen_captcha()), 401

        # 3) 本地凭据校验
        if user is not None and user.is_active and verify_password(password, user.hashed_password):
            ent = _enterprise_of(user)
            if ent is not None and (ent.status or "active") != "active":
                return jsonify(error="所属企业已停用，请联系平台管理员"), 403

            # 企业版入口：账号必须属于该企业；平台账号不能从企业入口登录
            if login_ent is not None:
                if ent is None:
                    return jsonify(error="这是平台账号，请从个人版入口登录"), 403
                if ent.id != login_ent.id:
                    return jsonify(error=f"该账号不属于「{login_ent.name}」，请确认企业编码"), 403
            user.failed_attempts = 0
            user.locked_until = None
            db.session.commit()
            token = create_access_token(user.id)
            return jsonify(access_token=token, user=_user_payload(user))

    # 4) 门户回落（仅非管理员；影子账号直接到这里）
    #    注意：本地真实账号（非影子）密码错就是密码错，不能再去问门户——
    #    否则门户不可达时会返回 503「统一门户暂时不可用」，
    #    用户看到的是一句与密码无关的报错（实测踩到过）。
    if not is_admin and (user is None or is_shadow):
        from ..portal import portal_login
        res = portal_login(username, password)
        if res is not None and res.get("ok"):
            from ..portal_provision import resolve_or_create_shadow
            su = resolve_or_create_shadow(res.get("user_id") or "",
                                          res.get("username") or username)
            if su is None:
                return jsonify(error="门户账号本地建档失败，请联系管理员"), 500
            ent = _enterprise_of(su)
            if ent is not None and (ent.status or "active") != "active":
                return jsonify(error="所属企业已停用，请联系平台管理员"), 403
            token = create_access_token(su.id)
            return jsonify(access_token=token, user=_user_payload(su))
        if res is not None and res.get("error") == "disabled":
            return jsonify(error="该账号已被禁用，请联系管理员"), 401
        if res is not None and res.get("error") == "unavailable":
            return jsonify(error="统一门户暂时不可用，请稍后重试"), 503
        # res 为 None（未配置门户）或 invalid/param -> 走标准失败

    # 5) 标准失败（影子账号不累计本地失败）
    if user is not None and not is_shadow:
        user.failed_attempts = (user.failed_attempts or 0) + 1
        if user.failed_attempts >= MAX_FAILS:
            user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
            user.failed_attempts = 0
            db.session.commit()
            return jsonify(error=f"连续失败次数过多，账号已锁定 {LOCK_MINUTES} 分钟"), 423
        db.session.commit()
        again = (user.failed_attempts or 0) >= CAPTCHA_AFTER
        return jsonify(error="用户名或密码错误", need_captcha=again,
                       captcha=_gen_captcha() if again else None), 401
    return jsonify(error="用户名或密码错误"), 401


@auth_bp.get("/auth/me")
def me():
    user = _resolve_user()
    if user is None or not user.is_active:
        return jsonify(error="登录已失效，请重新登录"), 401
    return jsonify(_user_payload(user))


@auth_bp.post("/auth/refresh")
def refresh():
    """用旧 token 换新的（未过期即可）。"""
    user = _resolve_user()
    if user is None or not user.is_active:
        return jsonify(error="登录已失效，请重新登录"), 401
    return jsonify(access_token=create_access_token(user.id))


@auth_bp.post("/auth/register")
def register():
    """自助注册：默认创建 viewer 角色账号，校验密码复杂度后返回 token（自动登录）。

    说明：企业内平台注册通常应仅限管理员开放；此处按任务书「登录/注册页面」
    实现自助注册，默认最低权限 viewer，并在 README 标注生产应改为邀请制。
    """
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password", "")
    if not username:
        return jsonify(error="用户名必填"), 400
    if db.session.query(User).filter_by(username=username).first():
        return jsonify(error="用户名已存在"), 409

    ok, msg = validate_password(password)
    if not ok:
        return jsonify(error=msg), 400

    user = User(username=username, hashed_password=hash_password(password),
                role="viewer", is_active=True)
    db.session.add(user)
    db.session.commit()
    record_audit(user.id, "register_user", "user", user.id,
                {"username": username, "role": "viewer"})
    token = create_access_token(user.id)
    return jsonify(access_token=token, user=user.to_dict()), 201


@auth_bp.post("/auth/forgot-password")
def forgot_password():
    """找回密码第一步：按用户名生成 6 位重置验证码（10 分钟有效）。

    说明：当前无短信/邮件通道，验证码在响应中返回（debug_code）便于演示；
    生产环境接入短信/邮件后应删除 debug_code，改为静默发送。
    """
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify(error="用户名必填"), 400

    user = db.session.query(User).filter_by(username=username).first()
    now = _utcnow()
    prev = _reset_store.get(username)
    if prev and prev.get("last_sent_at"):
        since = now - prev["last_sent_at"]
        if since < RESET_SEND_COOLDOWN:
            wait = int((RESET_SEND_COOLDOWN - since).total_seconds()) + 1
            return jsonify(error=f"发送过于频繁，请 {wait} 秒后重试"), 429

    # 用户不存在也返回成功形态，避免账号枚举；debug_code 为空
    if user is None:
        return jsonify(sent=True, note="若该账号存在，验证码已发送"), 200

    code = f"{random.randint(0, 999999):06d}"
    _reset_store[username] = {
        "code": code,
        "expires_at": now + RESET_CODE_TTL,
        "attempts": 0,
        "last_sent_at": now,
    }
    record_audit(user.id, "forgot_password", "user", user.id, {"username": username})
    return jsonify(
        sent=True,
        expires_in=int(RESET_CODE_TTL.total_seconds()),
        debug_code=code,
        note="验证码已生成（测试环境直接展示；生产环境将通过短信/邮件发送）",
    ), 200


@auth_bp.post("/auth/reset-password")
def reset_password():
    """找回密码第二步：校验验证码并重置密码。验证码错误上限 5 次。"""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    code = (data.get("code") or "").strip()
    new_password = data.get("new_password", "")
    if not username or not code or not new_password:
        return jsonify(error="用户名、验证码和新密码必填"), 400

    rec = _reset_store.get(username)
    now = _utcnow()
    if rec is None:
        return jsonify(error="验证码不存在或已失效，请重新获取"), 400
    if now > rec["expires_at"]:
        _reset_store.pop(username, None)
        return jsonify(error="验证码已过期，请重新获取"), 400
    if rec["attempts"] >= RESET_MAX_ATTEMPTS:
        _reset_store.pop(username, None)
        return jsonify(error="尝试次数过多，请重新获取验证码"), 400

    user = db.session.query(User).filter_by(username=username).first()
    if user is None:
        _reset_store.pop(username, None)
        return jsonify(error="账号不存在"), 400
    if str(rec["code"]) != code:
        rec["attempts"] += 1
        remain = RESET_MAX_ATTEMPTS - rec["attempts"]
        if remain <= 0:
            _reset_store.pop(username, None)
        return jsonify(error=f"验证码错误，还可尝试 {remain} 次"), 400

    ok, msg = validate_password(new_password)
    if not ok:
        return jsonify(error=msg), 400

    user.hashed_password = hash_password(new_password)
    user.failed_attempts = 0
    user.locked_until = None
    db.session.commit()
    _reset_store.pop(username, None)
    record_audit(user.id, "reset_password", "user", user.id, {"username": username})
    return jsonify(ok=True, message="密码已重置，请使用新密码登录")
