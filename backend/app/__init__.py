"""Flask 应用工厂。

启动：
    flask --app run run   （run.py 里注册了 flask run）
或：
    python run.py
"""
import os

from flask import Flask, jsonify, send_file, send_from_directory
from .config import Config
from .extensions import db, cors


def _frontend_dist():
    """前端打包产物目录。

    部署到服务器时，后端用 Flask 同源托管前端（nginx 只需把 / 反代过来即可），
    跟原来 122 上的部署方式保持一致。本地开发用 vite dev server，这里不存在 dist
    就自动跳过托管，不影响 API。
    """
    env = os.environ.get("FRONTEND_DIST")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", "frontend", "dist"))


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # 初始化扩展
    db.init_app(app)
    cors.init_app(app, origins=app.config["CORS_ORIGINS"],
                  supports_credentials=True)

    # WebSocket 实时视频流
    from .stream import sock
    sock.init_app(app)

    # WebRTC 视频推流（aiortc）—— 本机无 aiortc/av 时跳过（不影响 WS 投屏）
    try:
        from .webrtc import webrtc_bp
        app.register_blueprint(webrtc_bp, url_prefix="/api")
    except Exception:
        pass

    # 注册路由蓝图
    from .routes import (auth_bp, users_bp, devices_bp, tasks_bp, dashboard_bp,
                         audit_bp, files_bp, scripts_bp, groups_bp, alerts_bp, exports_bp, settings_bp, images_bp, ssh_bridge_bp, billing_bp, servers_bp, vpc_bp, vpc_lines_bp, streams_bp, internal_bp, org_bp, enterprises_bp)
    from .routes.diagnostics import diag_bp as diagnostics_bp  # 诊断修复
    app.register_blueprint(auth_bp, url_prefix="/api")
    app.register_blueprint(users_bp, url_prefix="/api")
    app.register_blueprint(devices_bp, url_prefix="/api")
    app.register_blueprint(tasks_bp, url_prefix="/api")
    app.register_blueprint(dashboard_bp, url_prefix="/api")
    app.register_blueprint(audit_bp, url_prefix="/api")
    app.register_blueprint(files_bp, url_prefix="/api")
    app.register_blueprint(scripts_bp, url_prefix="/api")
    app.register_blueprint(groups_bp, url_prefix="/api")
    app.register_blueprint(alerts_bp, url_prefix="/api")
    app.register_blueprint(exports_bp, url_prefix="/api")
    app.register_blueprint(settings_bp, url_prefix="/api")
    app.register_blueprint(images_bp, url_prefix="/api")
    app.register_blueprint(ssh_bridge_bp, url_prefix="/api")
    app.register_blueprint(billing_bp, url_prefix="/api")
    app.register_blueprint(servers_bp, url_prefix="/api")
    app.register_blueprint(vpc_bp, url_prefix="/api")
    app.register_blueprint(vpc_lines_bp, url_prefix="/api")
    app.register_blueprint(streams_bp, url_prefix="/api")  # 流媒体（转发流）
    app.register_blueprint(internal_bp, url_prefix="/api")  # 统一门户内网接口（门户 → 产品）
    app.register_blueprint(org_bp, url_prefix="/api")        # 部门与角色管理
    app.register_blueprint(enterprises_bp, url_prefix="/api")  # 企业管理（平台级）
    app.register_blueprint(diagnostics_bp, url_prefix="/api")

    # 设备归属隔离：管理员可见全部；普通用户仅可见自己名下的设备（且需有效套餐）
    from .scoping import enforce_tenant_scope
    enforce_tenant_scope(app)

    # 企业硬隔离：ORM 查询自动按企业过滤 + 写入自动打标/越权拦截
    from .tenant import init_app as init_tenant
    init_tenant(app)

    # 确保上传目录存在（文件管理模块）
    import os
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    # 启动后台调度器（APScheduler），仅当应用真正运行时拉起
    with app.app_context():
        from . import models  # noqa: F401  确保模型被导入，建表用
        db.create_all()
        # 轻量迁移：rotation_config 表补 devices_per_round 列（老库升级）
        from sqlalchemy import text, inspect as sa_inspect
        _insp = sa_inspect(db.engine)
        if "rotation_config" in _insp.get_table_names():
            _cols = {c["name"] for c in _insp.get_columns("rotation_config")}
            if "devices_per_round" not in _cols:
                with db.engine.begin() as _conn:
                    _conn.execute(text(
                        "ALTER TABLE rotation_config "
                        "ADD COLUMN devices_per_round INTEGER DEFAULT 2 NOT NULL"
                    ))
        if "scheduled_tasks" in _insp.get_table_names():
            _tcols = {c["name"] for c in _insp.get_columns("scheduled_tasks")}
            if "cron_expr" not in _tcols:
                with db.engine.begin() as _conn:
                    _conn.execute(text("ALTER TABLE scheduled_tasks ADD COLUMN cron_expr VARCHAR(64) DEFAULT ''"))
        if "users" in _insp.get_table_names():
            _ucols = {c["name"] for c in _insp.get_columns("users")}
            if "failed_attempts" not in _ucols:
                with db.engine.begin() as _conn:
                    _conn.execute(text("ALTER TABLE users ADD COLUMN failed_attempts INTEGER DEFAULT 0"))
            if "locked_until" not in _ucols:
                with db.engine.begin() as _conn:
                    _conn.execute(text("ALTER TABLE users ADD COLUMN locked_until DATETIME"))
        # devices 表补虚拟摄像头字段（PC 摄像头 → 云手机虚拟推流）
        if "devices" in _insp.get_table_names():
            _dcols = {c["name"] for c in _insp.get_columns("devices")}
            if "virtual_camera_url" not in _dcols:
                with db.engine.begin() as _conn:
                    _conn.execute(text(
                        "ALTER TABLE devices ADD COLUMN virtual_camera_url VARCHAR(512) DEFAULT ''"))
            if "virtual_camera_updated_at" not in _dcols:
                with db.engine.begin() as _conn:
                    _conn.execute(text(
                        "ALTER TABLE devices ADD COLUMN virtual_camera_updated_at DATETIME"))
        # plans 表补"统一门户"字段（必须在 seed_plans 之前，否则 ORM 查新列会 500）
        if "plans" in _insp.get_table_names():
            _pcols = {c["name"] for c in _insp.get_columns("plans")}
            _portal_plan_cols = {
                "sku_id": "VARCHAR(64)",
                "billing_mode": "INTEGER DEFAULT 1",
                "period_hours": "INTEGER",
                "highlights": "TEXT DEFAULT '[]'",
                "details": "TEXT DEFAULT '[]'",
                "badge": "VARCHAR(16) DEFAULT ''",
                "alloc_ips": "TEXT DEFAULT '[]'",
            }
            for _cn, _ct in _portal_plan_cols.items():
                if _cn not in _pcols:
                    with db.engine.begin() as _conn:
                        _conn.execute(text("ALTER TABLE plans ADD COLUMN %s %s" % (_cn, _ct)))
            try:
                with db.engine.begin() as _conn:
                    _conn.execute(text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS ix_plans_sku_id ON plans(sku_id)"))
            except Exception:  # noqa: BLE001
                pass
            # 计费周期单位：天 → 小时（2026-10-14 变更）。老库若曾有 period_days，×24 回填
            if "period_days" in _pcols:
                try:
                    with db.engine.begin() as _conn:
                        _conn.execute(text(
                            "UPDATE plans SET period_hours = period_days * 24 "
                            "WHERE period_hours IS NULL AND period_days IS NOT NULL"))
                except Exception:  # noqa: BLE001
                    pass
        # portal_provisions 表补 period_hours（计费周期单位：天 → 小时）
        if "portal_provisions" in _insp.get_table_names():
            _prcols = {c["name"] for c in _insp.get_columns("portal_provisions")}
            if "period_hours" not in _prcols:
                with db.engine.begin() as _conn:
                    _conn.execute(text(
                        "ALTER TABLE portal_provisions ADD COLUMN period_hours INTEGER DEFAULT 720"))
                if "period_days" in _prcols:
                    try:
                        with db.engine.begin() as _conn:
                            _conn.execute(text(
                                "UPDATE portal_provisions SET period_hours = period_days * 24 "
                                "WHERE period_days IS NOT NULL"))
                    except Exception:  # noqa: BLE001
                        pass
        # devices 表补 sub_id（设备归属的订阅/套餐）
        if "devices" in _insp.get_table_names():
            _dcols = {c["name"] for c in _insp.get_columns("devices")}
            if "sub_id" not in _dcols:
                with db.engine.begin() as _conn:
                    _conn.execute(text("ALTER TABLE devices ADD COLUMN sub_id INTEGER"))
        # orders 表补 source（订单来源：web=云手机平台 / portal=统一门户）
        if "orders" in _insp.get_table_names():
            _ocols = {c["name"] for c in _insp.get_columns("orders")}
            if "source" not in _ocols:
                with db.engine.begin() as _conn:
                    _conn.execute(text(
                        "ALTER TABLE orders ADD COLUMN source VARCHAR(16) DEFAULT 'web'"))
        # 先补列再跑种子数据：模型里新增的列（如 enterprise_id）在 seed 查询时就必须存在
        from .schema_guard import ensure_schema
        ensure_schema(app)
        from .tenant import ensure_default_enterprise
        try:
            ensure_default_enterprise()
        except Exception as _e:  # noqa: BLE001 - 企业初始化失败不应阻塞启动
            print(f"[tenant] 初始化失败: {_e}", flush=True)

        from .seed import seed
        seed()
        from .billing import seed_plans
        seed_plans()

        # 既有的企业管理员如果还没配过功能权限，给一份「接近系统级」兜底
        try:
            from . import perms as _perms
            from .models import User as _User
            _perms.ensure_builtin_presets()
            _synced = _perms.sync_builtin_assignments()
            if _synced:
                print(f"[perms] 已同步 {_synced} 个企业账号的档位权限", flush=True)
            for _u in db.session.query(_User).filter(
                _User.enterprise_id.isnot(None),
                _User.role.in_(("admin", "superadmin")),
            ).all():
                _perms.ensure_default_for(_u)
        except Exception as _e:  # noqa: BLE001
            print(f"[perms] 初始化失败: {_e}", flush=True)
        from .scheduler import start_scheduler
        start_scheduler(app)

    @app.get("/api/health")
    def health():
        return jsonify(status="ok", backend=app.config["DEVICE_BACKEND"])

    # ---- 同源托管前端（存在 dist 时生效）----
    # SPA 需要把未知路径回退到 index.html；/api/*、/ws/* 由各自的蓝图先匹配，
    # 不会被这里的通配规则截走（Werkzeug 优先匹配更具体的静态规则）。
    dist = _frontend_dist()
    if os.path.isfile(os.path.join(dist, "index.html")):
        @app.get("/")
        def root():
            return send_file(os.path.join(dist, "index.html"))

        # 静态资源的"扩展名白名单"：这些前缀/后缀是真实文件，找不到就该 404，
        # 不能回退到 index.html（否则 /downloads/xxx.apk 会返回 200 + HTML，测试报告 ENT-003）
        _ASSET_PREFIXES = ("assets/", "downloads/", "uploads/")
        _ASSET_EXTS = (".js", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                       ".ico", ".webp", ".woff", ".woff2", ".ttf", ".apk", ".exe",
                       ".zip", ".wasm", ".json", ".txt", ".pdf")

        @app.get("/<path:sub>")
        def spa_static(sub):
            full = os.path.join(dist, sub)
            if os.path.isfile(full):
                return send_from_directory(dist, sub)

            # 安装包目录：本地没有真实文件时明确 404，而不是给一页 HTML
            if sub.startswith("downloads/"):
                return jsonify(error="安装包暂未发布：请把 APK/EXE 放到 frontend/public/downloads/ 后重新构建",
                               path="/" + sub), 404

            looks_like_asset = sub.startswith(_ASSET_PREFIXES) or sub.lower().endswith(_ASSET_EXTS)
            if looks_like_asset:
                return jsonify(error="资源不存在", path="/" + sub), 404

            return send_file(os.path.join(dist, "index.html"))
    else:
        @app.get("/")
        def root():
            return jsonify(name=app.config["APP_NAME"],
                            backend=app.config["DEVICE_BACKEND"],
                            docs="后端无 Swagger，但路由见 routes/")

    return app
