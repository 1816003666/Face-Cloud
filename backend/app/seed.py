"""首次启动初始化：创建默认分组 + 初始管理员账号（幂等）。"""
from .extensions import db
from .models import User, Group
from .auth import hash_password


def seed():
    def ensure_user(username, password, role):
        u = db.session.query(User).filter_by(username=username).first()
        if u is None:
            u = User(username=username, hashed_password=hash_password(password),
                     role=role, is_active=True)
            db.session.add(u)
        else:
            # 已存在则补齐角色，方便重置环境
            u.role = role
            u.is_active = True
        return u

    from flask import current_app
    cfg = current_app.config
    ensure_user(cfg["SEED_ADMIN_USERNAME"], cfg["SEED_ADMIN_PASSWORD"], "admin")
    root = ensure_user(cfg["SEED_SUPERADMIN_USERNAME"], cfg["SEED_SUPERADMIN_PASSWORD"], "superadmin")
    db.session.flush()  # 确保 root.id 已生成

    # 默认分组（归属超级管理员）
    grp = db.session.query(Group).filter_by(name="默认分组").first()
    if grp is None:
        db.session.add(Group(name="默认分组", description="系统初始化分组", owner_id=root.id))
    else:
        grp.owner_id = root.id

    db.session.commit()
