"""数据库架构：8 张表。

ER 关系：
    users 1──* groups            (owner)
    groups 1──* devices
    users 1──* devices           (creator)
    users 1──* scripts           (owner)
    users 1──* scheduled_tasks   (creator)
    users 1──* audit_logs        (actor)
    scheduled_tasks 1──* task_executions
    devices 1──* device_logs
"""
import json
import os
from datetime import datetime, timedelta
from flask import current_app
from flask_sqlalchemy import SQLAlchemy

# 为避免循环依赖，这里直接复用 extensions 的 db 实例
from .extensions import db  # noqa: E402


def iso_utc(dt):
    """把库里 naive UTC 时间输出成带 Z 的 ISO 串（前端 new Date 才能正确转本地时区）。

    毫秒精度：JS 的 Date 解析对超过 3 位小数不保证，这里统一裁到毫秒。
    """
    if dt is None:
        return None
    try:
        return dt.isoformat(timespec="milliseconds") + "Z"
    except (TypeError, ValueError):
        return dt.isoformat() + "Z"


def _utcnow():
    # 统一用 naive UTC，避免 SQLite/Postgres 时区混用导致比较报错
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# 枚举约束（用字符串列 + Python 常量，便于 SQLite/Postgres 通用，免迁移踩坑）
# ---------------------------------------------------------------------------
ROLE_LEVELS = {
    "viewer": 1,
    "operator": 2,
    "admin": 3,
    "superadmin": 4,
}
ROLE_CHOICES = list(ROLE_LEVELS.keys())

DEVICE_STATUS = ["creating", "running", "stopped", "error"]
DEVICE_BACKEND = ["simulator", "redroid"]
SCHEDULE_TYPE = ["once", "interval", "cron"]
TASK_ACTION = ["open_url", "tap", "swipe", "text", "key", "install", "sequence", "wait", "health_check", "run_script"]

# 仅管理员（admin / superadmin）可创建、执行、查看的任务动作。
# 健康巡检会读到平台级运维信息（服务器 CPU/磁盘/容器、全量云手机明细），
# 属于运维工具而不是终端用户功能，所以默认只对管理员开放。
PRIVILEGED_TASK_ACTIONS = frozenset({"health_check"})
EXEC_STATUS = ["pending", "running", "success", "failed"]


def role_level(role: str) -> int:
    return ROLE_LEVELS.get(role, 0)


def record_audit(actor_id: int, action: str, target_type: str = "",
                 target_id: int = None, detail: dict = None):
    """写入操作审计（权限系统标配）。detail 传 dict 会自动 JSON 化。"""
    import json
    log = AuditLog(
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=json.dumps(detail or {}, ensure_ascii=False),
    )
    db.session.add(log)
    db.session.commit()


# ---------------------------------------------------------------------------
# 用户与权限
# ---------------------------------------------------------------------------
class Enterprise(db.Model):
    """企业（租户）。一套系统里承载多家企业各自运营。

    - 平台超管 enterprise_id 为空，可跨企业管理；
    - 企业管理员/成员挂在某个企业下，只能看本企业的数据；
    - devices.enterprise_id 为空表示公共设备池（平台共享），
      分配给某企业用户后写入该企业，回收时清空。
    """
    __tablename__ = "enterprises"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    code = db.Column(db.String(32), unique=True, nullable=False)
    status = db.Column(db.String(16), default="active")   # active / disabled
    contact = db.Column(db.String(64), default="")
    remark = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=_utcnow)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "code": self.code,
                "status": self.status or "active", "contact": self.contact or "",
                "remark": self.remark or "",
                "created_at": self.created_at.isoformat() if self.created_at else None}


class User(db.Model):
    __tablename__ = "users"
    # 所属企业（NULL = 平台级：平台超管账号、公共设备池、平台通用套餐）
    enterprise_id = db.Column(db.Integer, db.ForeignKey("enterprises.id"),
                              nullable=True, index=True)

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)   # 登录名（唯一）
    # 展示名：门户影子账号用门户昵称，界面上就不会出现 portal_<uuid>
    display_name = db.Column(db.String(64), nullable=True)
    hashed_password = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(16), nullable=False, default="viewer")
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    failed_attempts = db.Column(db.Integer, default=0)          # 连续登录失败次数
    locked_until = db.Column(db.DateTime, nullable=True)      # 失败锁定截止时间
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    groups = db.relationship("Group", back_populates="owner",
                             foreign_keys="Group.owner_id")
    devices = db.relationship("Device", back_populates="creator",
                              foreign_keys="Device.created_by")
    scripts = db.relationship("Script", back_populates="owner")
    scheduled_tasks = db.relationship("ScheduledTask", back_populates="creator")
    audit_logs = db.relationship("AuditLog", back_populates="actor")

    def to_dict(self):
        return {
            "display_name": self.display_name or "",
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "is_active": self.is_active,
            "enterprise_id": self.enterprise_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ---------------------------------------------------------------------------
# 设备分组
# ---------------------------------------------------------------------------
class Group(db.Model):
    __tablename__ = "groups"
    # 所属企业（NULL = 平台级：平台超管账号、公共设备池、平台通用套餐）
    enterprise_id = db.Column(db.Integer, db.ForeignKey("enterprises.id"),
                              nullable=True, index=True)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False, index=True)
    description = db.Column(db.String(255), default="")
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    owner = db.relationship("User", back_populates="groups",
                            foreign_keys=[owner_id])
    devices = db.relationship("Device", back_populates="group")


# ---------------------------------------------------------------------------
# 云手机设备
# ---------------------------------------------------------------------------
class Device(db.Model):
    __tablename__ = "devices"
    # 所属企业（NULL = 平台级：平台超管账号、公共设备池、平台通用套餐）
    enterprise_id = db.Column(db.Integer, db.ForeignKey("enterprises.id"),
                              nullable=True, index=True)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False, index=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=True)
    status = db.Column(db.String(16), nullable=False, default="creating")
    serial = db.Column(db.String(64), default="")        # adb serial / simulator id
    backend = db.Column(db.String(16), default="simulator")
    ip = db.Column(db.String(64), default="")            # 出口 IP（群控卖点）
    fingerprint = db.Column(db.Text, default="")         # 设备指纹 JSON
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    # 归属的订阅（套餐）：分配设备时写入，用于区分"哪台属于哪个套餐"
    sub_id = db.Column(db.Integer, db.ForeignKey("subscriptions.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    # 告警系统用：最近一次心跳/操控时间 + 模拟资源指标（redroid 接入后由真实采集替换）
    last_seen = db.Column(db.DateTime, nullable=True)
    cpu = db.Column(db.Float, nullable=True)             # 百分比 0-100
    mem = db.Column(db.Float, nullable=True)             # 百分比 0-100
    egress_ip = db.Column(db.String(64), default="")   # SOCKS5 出口 IP（探测结果）
    # 绑定的出口线路（vpc_lines.id）；为空表示直连
    egress_line_id = db.Column(db.Integer, nullable=True, index=True)
    # PC 摄像头虚拟推流：当前虚拟摄像头源地址（HLS）+ 更新时间
    virtual_camera_url = db.Column(db.String(512), default="")
    virtual_camera_updated_at = db.Column(db.DateTime, nullable=True)
    # 云手机的规格（由创建时所用套餐写入）：用于"套餐配置 vs 后台现有云手机配置"的匹配判断
    spec_resolution = db.Column(db.String(32), default="")
    spec_cpu = db.Column(db.String(32), default="")
    spec_memory = db.Column(db.String(32), default="")
    spec_storage = db.Column(db.String(32), default="")
    spec_android = db.Column(db.String(32), default="")

    group = db.relationship("Group", back_populates="devices")
    creator = db.relationship("User", back_populates="devices",
                              foreign_keys=[created_by])
    logs = db.relationship("DeviceLog", back_populates="device",
                           cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# 设备日志
# ---------------------------------------------------------------------------
class DeviceLog(db.Model):
    __tablename__ = "device_logs"

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.Integer, db.ForeignKey("devices.id"), nullable=False)
    level = db.Column(db.String(16), default="info")
    message = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=_utcnow)

    device = db.relationship("Device", back_populates="logs")


# ---------------------------------------------------------------------------
# 定时任务定义
# ---------------------------------------------------------------------------
class ScheduledTask(db.Model):
    __tablename__ = "scheduled_tasks"
    # 所属企业（NULL = 平台级：平台超管账号、公共设备池、平台通用套餐）
    enterprise_id = db.Column(db.Integer, db.ForeignKey("enterprises.id"),
                              nullable=True, index=True)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    action = db.Column(db.String(32), nullable=False)     # 见 TASK_ACTION
    params = db.Column(db.Text, default="{}")             # JSON 参数
    device_ids = db.Column(db.Text, default="[]")         # JSON 数组
    schedule_type = db.Column(db.String(16), default="once")  # once/interval/cron
    interval_seconds = db.Column(db.Integer, default=3600)
    cron_expr = db.Column(db.String(64), default="")          # cron 表达式（如 "0 2 * * *"）
    next_run = db.Column(db.DateTime, nullable=True)
    enabled = db.Column(db.Boolean, default=True)
    # 失败重试次数：执行时某台设备失败，最多再重试这么多次（0=不重试）
    max_retries = db.Column(db.Integer, default=0)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    creator = db.relationship("User", back_populates="scheduled_tasks")
    executions = db.relationship("TaskExecution", back_populates="task",
                                 cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# 任务执行记录
# ---------------------------------------------------------------------------
class TaskExecution(db.Model):
    __tablename__ = "task_executions"

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("scheduled_tasks.id"), nullable=False)
    status = db.Column(db.String(16), default="pending")
    started_at = db.Column(db.DateTime, default=_utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)
    total = db.Column(db.Integer, default=0)
    ok = db.Column(db.Integer, default=0)
    failed = db.Column(db.Integer, default=0)
    detail = db.Column(db.Text, default="")               # JSON 明细

    task = db.relationship("ScheduledTask", back_populates="executions")


# ---------------------------------------------------------------------------
# 操作审计
# ---------------------------------------------------------------------------
class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    # 所属企业（NULL = 平台级：平台超管账号、公共设备池、平台通用套餐）
    enterprise_id = db.Column(db.Integer, db.ForeignKey("enterprises.id"),
                              nullable=True, index=True)

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    action = db.Column(db.String(64), nullable=False)     # create_user/delete_device...
    target_type = db.Column(db.String(32), default="")
    target_id = db.Column(db.Integer, nullable=True)
    detail = db.Column(db.Text, default="{}")             # JSON
    created_at = db.Column(db.DateTime, default=_utcnow)

    actor = db.relationship("User", back_populates="audit_logs")


# ---------------------------------------------------------------------------
# 云手机轮次运行配置（24 小时分 N 轮，每轮结束销毁重建）
# ---------------------------------------------------------------------------
class RotationConfig(db.Model):
    """轮次运行模式（单行配置，id 固定为 1）。

    enabled 开启后，调度器按「每轮时长 = 24h / rounds」自动轮转：
    每轮结束销毁所有 redroid 容器并按原数量/名称重新创建。
    """
    __tablename__ = "rotation_config"

    id = db.Column(db.Integer, primary_key=True)          # 固定为 1
    enabled = db.Column(db.Boolean, default=False, nullable=False)
    rounds = db.Column(db.Integer, default=4, nullable=False)   # 一天分几轮（2..24）
    devices_per_round = db.Column(db.Integer, default=2, nullable=False)  # 每轮创建的云手机数量
    round_index = db.Column(db.Integer, default=1, nullable=False)  # 当前第几轮
    started_at = db.Column(db.DateTime, nullable=True)    # 开启时刻
    next_round_at = db.Column(db.DateTime, nullable=True)  # 下次轮转时间
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)


class UserRotation(db.Model):
    """普通用户的轮次运行配置（每人一行）。

    每一轮 = 把该用户名下最多 devices_per_round 台云手机「清数据 + 收回公共池」，
    再从池子里重新分配同样台数给他；不销毁容器。
    """
    __tablename__ = "user_rotations"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True,
                        nullable=False, index=True)
    enabled = db.Column(db.Boolean, default=False, nullable=False)
    rounds = db.Column(db.Integer, default=4, nullable=False)
    devices_per_round = db.Column(db.Integer, default=2, nullable=False)
    round_index = db.Column(db.Integer, default=1, nullable=False)
    started_at = db.Column(db.DateTime, nullable=True)
    next_round_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)


# ---------------------------------------------------------------------------
# 组织架构：部门 / 角色 / 成员档案（对应前端「部门与角色」页面）
# ---------------------------------------------------------------------------
class Department(db.Model):
    """部门树（parent_id 为空即根部门）。"""
    __tablename__ = "departments"
    # 所属企业（NULL = 平台级：平台超管账号、公共设备池、平台通用套餐）
    enterprise_id = db.Column(db.Integer, db.ForeignKey("enterprises.id"),
                              nullable=True, index=True)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True, index=True)
    sort = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=_utcnow)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "parent_id": self.parent_id,
                "sort": self.sort or 0}


class OrgRole(db.Model):
    """组织角色：只承载功能权限（perms），数据范围按成员单独配置。"""
    __tablename__ = "org_roles"
    # 所属企业（NULL = 平台级：平台超管账号、公共设备池、平台通用套餐）
    enterprise_id = db.Column(db.Integer, db.ForeignKey("enterprises.id"),
                              nullable=True, index=True)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    code = db.Column(db.String(64), default="")
    description = db.Column(db.String(255), default="")
    perms = db.Column(db.Text, default="[]")     # JSON 数组，如 ["res.view","bill.view"]
    sort = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=_utcnow)

    def to_dict(self):
        import json as _json
        try:
            perms = _json.loads(self.perms or "[]")
        except (TypeError, ValueError):
            perms = []
        return {"id": self.id, "name": self.name, "code": self.code or "",
                "description": self.description or "", "perms": perms,
                "sort": self.sort or 0}


class PermPreset(db.Model):
    """功能权限模板（档位）：平台管理员可自定义"标签"，比如「客服专员」。

    builtin=True 的是内置档位（基础/标准/高级/接近系统级），可改内容不可删。
    """
    __tablename__ = "perm_presets"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(32), nullable=False)
    desc = db.Column(db.String(128), default="")
    perms = db.Column(db.Text, default="[]")
    builtin = db.Column(db.Boolean, default=False)
    sort = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=_utcnow)

    def to_dict(self):
        import json as _json
        try:
            codes = _json.loads(self.perms or "[]")
        except (TypeError, ValueError):
            codes = []
        return {"id": self.id, "code": self.code, "name": self.name,
                "desc": self.desc or "", "perms": codes, "count": len(codes),
                "builtin": bool(self.builtin), "sort": self.sort or 0}


class UserPerm(db.Model):
    """账号级功能权限（覆盖/补充组织角色）。

    用途：平台管理员给「企业管理员」授权能用到什么程度，
    最高可以给到接近系统级（除平台专属之外的全部功能）。
    """
    __tablename__ = "user_perms"

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    perms = db.Column(db.Text, default="[]")     # JSON 数组
    preset = db.Column(db.String(32), default="")  # 预设名（basic/standard/advanced/near_system）
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)


class OrgMember(db.Model):
    """成员的组织档案：部门、角色、数据范围；身份仍以 users 表为准。"""
    __tablename__ = "org_members"

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    display_name = db.Column(db.String(64), default="")
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True)
    role_ids = db.Column(db.Text, default="[]")   # JSON 数组
    scope = db.Column(db.String(16), default="self")  # all / dept_sub / dept / self
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)


# ---------------------------------------------------------------------------
# 脚本模板（跨设备回放）
# ---------------------------------------------------------------------------
class Script(db.Model):
    __tablename__ = "scripts"
    # 所属企业（NULL = 平台级：平台超管账号、公共设备池、平台通用套餐）
    enterprise_id = db.Column(db.Integer, db.ForeignKey("enterprises.id"),
                              nullable=True, index=True)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    steps = db.Column(db.Text, default="[]")              # JSON 步骤数组
    # 参数化变量默认值：{"账号": "a@b.com"}，步骤里用 ${账号} 引用
    variables = db.Column(db.Text, default="{}")
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    owner = db.relationship("User", back_populates="scripts")
    executions = db.relationship("ScriptExecution", back_populates="script",
                                 cascade="all, delete-orphan")


# 脚本执行历史（每次执行的设备明细）
class ScriptExecution(db.Model):
    __tablename__ = "script_executions"

    id = db.Column(db.Integer, primary_key=True)
    script_id = db.Column(db.Integer, db.ForeignKey("scripts.id"), nullable=False)
    script_name = db.Column(db.String(128), default="")   # 冗余快照（脚本删除后仍可读）
    status = db.Column(db.String(16), default="success")  # success / partial / failed
    total = db.Column(db.Integer, default=0)
    ok = db.Column(db.Integer, default=0)
    failed = db.Column(db.Integer, default=0)
    detail = db.Column(db.Text, default="[]")             # JSON 明细 [{device_id,device_name,serial,ok,message}]
    executed_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    script = db.relationship("Script", back_populates="executions")


# ---------------------------------------------------------------------------
# 文件管理（上传/下载/浏览/推送到设备）
# ---------------------------------------------------------------------------
class FileRecord(db.Model):
    __tablename__ = "files"
    # 所属企业（NULL = 平台级：平台超管账号、公共设备池、平台通用套餐）
    enterprise_id = db.Column(db.Integer, db.ForeignKey("enterprises.id"),
                              nullable=True, index=True)

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)          # 用户看到的原始文件名
    stored_name = db.Column(db.String(255), nullable=False)       # 磁盘上的实际文件名
    size = db.Column(db.Integer, default=0)
    mime = db.Column(db.String(128), default="")
    uploader_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    target_device_id = db.Column(db.Integer, db.ForeignKey("devices.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    uploader = db.relationship("User")


# ---------------------------------------------------------------------------
# 告警系统（任务书 #12）：设备离线 / 资源超限 / 操作失败 三类告警
# ---------------------------------------------------------------------------
ALERT_LEVELS = ["info", "warning", "critical"]
ALERT_TYPES = ["device_offline", "resource_limit", "operation_failure", "health_check"]
ALERT_STATUSES = ["active", "acknowledged", "resolved"]


def _notify_webhook(a):
    """告警产生时推送外部 webhook（飞书/企业微信/自建均可），后台线程发送不阻塞。"""
    try:
        url = (get_setting("alert_webhook_url")
               or current_app.config.get("ALERT_WEBHOOK_URL") or "").strip()
    except Exception:  # noqa: BLE001
        url = ""
    if not url:
        return
    import json as _json
    import threading
    import urllib.request
    payload = {
        "event": "alert",
        "type": a.type,
        "level": a.level,
        "message": a.message,
        "device_id": a.device_id,
        "status": a.status,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }

    def _send():
        req = urllib.request.Request(
            url, data=_json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_send, daemon=True).start()


def raise_alert(atype: str, level: str, message: str,
                device_id: int = None, detail: dict = None,
                dedup_minutes: int = 30):
    """写入一条告警。

    - 同一 (device_id, type) 在 dedup 窗口内已有未解决告警则跳过，避免刷屏；
    - 返回新建/已有的 Alert 对象，便于调用方继续处理。
    """
    import json
    now = _utcnow()
    existing = (db.session.query(Alert)
                .filter(Alert.type == atype,
                        Alert.device_id == device_id,
                        Alert.status != "resolved",
                        Alert.created_at >= now - timedelta(minutes=dedup_minutes))
                .first())
    if existing:
        return existing
    a = Alert(level=level, type=atype, message=message,
              device_id=device_id, status="active",
              detail=json.dumps(detail or {}, ensure_ascii=False))
    db.session.add(a)
    db.session.commit()
    _notify_webhook(a)
    return a


def resolve_alerts_for_device(device_id: int, atype: str = None,
                              actor_id: int = None):
    """自动恢复：把某设备的未解决告警标记为已解决（设备恢复在线时调用）。"""
    q = db.session.query(Alert).filter(
        Alert.device_id == device_id, Alert.status != "resolved")
    if atype:
        q = q.filter(Alert.type == atype)
    changed = 0
    for a in q.all():
        a.status = "resolved"
        a.resolved_at = _utcnow()
        a.resolved_by = actor_id
        changed += 1
    if changed:
        db.session.commit()
    return changed


class Alert(db.Model):
    __tablename__ = "alerts"
    # 所属企业（NULL = 平台级：平台超管账号、公共设备池、平台通用套餐）
    enterprise_id = db.Column(db.Integer, db.ForeignKey("enterprises.id"),
                              nullable=True, index=True)

    id = db.Column(db.Integer, primary_key=True)
    level = db.Column(db.String(16), nullable=False, default="warning")   # info/warning/critical
    type = db.Column(db.String(32), nullable=False)                       # 见 ALERT_TYPES
    message = db.Column(db.Text, default="")
    device_id = db.Column(db.Integer, db.ForeignKey("devices.id"), nullable=True)
    status = db.Column(db.String(16), nullable=False, default="active")   # active/acknowledged/resolved
    detail = db.Column(db.Text, default="{}")                            # JSON
    created_at = db.Column(db.DateTime, default=_utcnow)
    resolved_at = db.Column(db.DateTime, nullable=True)
    acknowledged_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    resolved_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)


# ---------------------------------------------------------------------------
# 资源/在线率历史采样（看板趋势图用，每 60s 一条）
# ---------------------------------------------------------------------------
class MetricsSnapshot(db.Model):
    __tablename__ = "metrics_snapshots"

    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.DateTime, default=_utcnow, index=True)
    server_cpu = db.Column(db.Float, nullable=True)     # 负载 ratio*100
    server_mem = db.Column(db.Float, nullable=True)     # 内存使用 %
    server_disk = db.Column(db.Float, nullable=True)    # 磁盘使用 %
    containers = db.Column(db.Integer, nullable=True)   # Docker 运行/总 数量
    devices_online = db.Column(db.Integer, nullable=True)
    devices_total = db.Column(db.Integer, nullable=True)


# ---------------------------------------------------------------------------
# 系统设置（key-value，阈值/通知地址等可视化配置）
# ---------------------------------------------------------------------------
SETTING_DEFAULTS = {
    "alert_offline_seconds": 120,     # 设备超过该秒数无心跳判离线
    "alert_cpu_limit": 90,            # 云手机 CPU 超限阈值 %
    "alert_mem_limit": 90,            # 云手机内存超限阈值 %
    "alert_webhook_url": "",          # 告警通知 webhook（飞书/企业微信/自建）
    "screenshot_interval": 5,         # 设备卡片截图轮询间隔（秒），前端按批错峰调度
}


class SystemConfig(db.Model):
    __tablename__ = "system_configs"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, default="")
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)


def get_setting(key, default=None):
    row = db.session.get(SystemConfig, key)
    return default if row is None else row.value


def set_setting(key, value):
    row = db.session.get(SystemConfig, key)
    if row is None:
        row = SystemConfig(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)
    db.session.commit()
    return row.value


# ---------------------------------------------------------------------------
# 预付费计费：套餐 / 订单 / 订阅 / 到期提醒（客户端计费采用预计费）
# ---------------------------------------------------------------------------
PLAN_DURATION_MONTHS = [1, 3]          # 包月 / 包季
ORDER_STATUS = ["pending", "paid", "cancelled", "expired"]
SUB_STATUS = ["active", "expired", "cancelled"]
NOTIFY_TYPES = ["expire_soon", "expired", "recharge_hint", "payment_success"]


class Plan(db.Model):
    """云手机套餐（预购）：包月 / 包季，一次购买一段有效期。

    用户购买后生成 Subscription（active 直到 expires_at），到期自动置 expired。
    """
    __tablename__ = "plans"
    # 所属企业（NULL = 平台级：平台超管账号、公共设备池、平台通用套餐）
    enterprise_id = db.Column(db.Integer, db.ForeignKey("enterprises.id"),
                              nullable=True, index=True)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)           # 标准版 / 旗舰版…
    model = db.Column(db.String(32), default="标准版")          # 机型规格名
    price_cents = db.Column(db.Integer, nullable=False)       # 单价（分）
    duration_months = db.Column(db.Integer, default=1)        # 1=包月 3=包季
    specs = db.Column(db.Text, default="{}")                  # JSON 规格（分辨率/CPU/内存/存储/安卓）
    # 套餐标称台数（specs.device_count 的镜像列，方便查询与后台展示；
    # 计价/起购/分配统一读 specs，写套餐时由 billing.sync_plan_device_count 同步）
    device_count = db.Column(db.Integer, default=1)
    sort = db.Column(db.Integer, default=0)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    # ---- 统一门户接入（product_code=phone）----
    sku_id = db.Column(db.String(64), unique=True, nullable=True)   # 门户商品号；空则该套餐不上架
    billing_mode = db.Column(db.Integer, default=1)                 # 1 包周期 2 按量
    period_hours = db.Column(db.Integer, nullable=True)              # 周期小时数；空则 duration_months*30*24（1天=24小时）
    highlights = db.Column(db.Text, default="[]")                   # JSON 数组：商品亮点
    details = db.Column(db.Text, default="[]")                      # JSON 数组：商品详情
    badge = db.Column(db.String(16), default="")                    # 门户角标文案
    alloc_ips = db.Column(db.Text, default="[]")                    # JSON 数组：可分配服务器 IP（空=默认池）

    orders = db.relationship("Order", back_populates="plan")

    def to_dict(self):
        import json
        try:
            specs = json.loads(self.specs or "{}")
        except (json.JSONDecodeError, TypeError):
            specs = {}
        hours = int(self.period_hours or 0) or max(1, int(self.duration_months or 1)) * 30 * 24
        period_label = (f"{hours} 小时（{hours // 24} 天）" if hours % 24 == 0 else f"{hours} 小时")
        try:
            server_ips = json.loads(self.alloc_ips or "[]")
        except (json.JSONDecodeError, TypeError):
            server_ips = []
        if not isinstance(server_ips, list):
            server_ips = []
        return {
            "id": self.id,
            "name": self.name,
            "model": self.model,
            "price_cents": self.price_cents,
            "price_yuan": round(self.price_cents / 100, 2),
            "duration_months": self.duration_months,
            "duration_label": f"{self.duration_months}个月",
            "period_hours": hours,
            "period_label": period_label,
            "sku_id": self.sku_id or "",
            "server_ips": server_ips,
            "specs": specs,
            # 台数存在 specs 里（Plan 表没有该列），这里提出来方便前端直接用
            "device_count": int(specs.get("device_count") or 1),
            "sort": self.sort,
            "active": self.active,
        }


class Order(db.Model):
    """支付订单（预付费）。

    channel: mock = 本地模拟支付（测试用）；alipay/wechat = 线上支付（需商户号）。
    out_trade_no 为系统生成的商户订单号；trade_no 为支付平台流水号。
    """
    __tablename__ = "orders"
    # 所属企业（NULL = 平台级：平台超管账号、公共设备池、平台通用套餐）
    enterprise_id = db.Column(db.Integer, db.ForeignKey("enterprises.id"),
                              nullable=True, index=True)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey("plans.id"), nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    # 本单购买了几「份」套餐（1 份 = 套餐标称台数 × 套餐周期）
    units = db.Column(db.Integer, default=0)
    # 本单购买的云手机台数与时长（小时）；历史订单为空时按套餐默认值解释
    qty = db.Column(db.Integer, default=0)
    hours = db.Column(db.Integer, default=0)
    unit_price_cents = db.Column(db.Integer, default=0)   # 单价（每台每小时，分）
    channel = db.Column(db.String(16), default="mock")        # mock/alipay/wechat
    source = db.Column(db.String(16), default="web")          # web=云手机平台自助 / portal=统一门户
    status = db.Column(db.String(16), default="pending")      # pending/paid/cancelled/expired
    out_trade_no = db.Column(db.String(64), unique=True, nullable=False)
    trade_no = db.Column(db.String(64), default="")           # 支付平台流水
    paid_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    # 用户从自己的订单列表里「删除」= 软隐藏：记录仍在，财务/对账不受影响
    hidden_by_user = db.Column(db.Boolean, default=False)

    user = db.relationship("User")
    plan = db.relationship("Plan", back_populates="orders")

    def to_dict(self):
        return {
            "units": self.units or 0,
            "id": self.id,
            "user_id": self.user_id,
            "plan_id": self.plan_id,
            "plan_name": self.plan.name if self.plan else "",
            "amount_cents": self.amount_cents,
            "amount_yuan": round(self.amount_cents / 100, 2),
            "qty": self.qty or 0,
            "hours": self.hours or 0,
            "unit_price_cents": self.unit_price_cents or 0,
            "channel": self.channel,
            "source": self.source or "web",
            "status": self.status,
            "out_trade_no": self.out_trade_no,
            "trade_no": self.trade_no,
            "paid_at": iso_utc(self.paid_at),
            "created_at": iso_utc(self.created_at),
        }


class Subscription(db.Model):
    """用户订阅（预付费套餐的有效期）。

    续费叠加：购买新套餐时，若已有 active 订阅则在其 expires_at 上叠加时长。
    到期（expires_at 过去且状态 active）由调度器统一置 expired 并断开设备。
    """
    __tablename__ = "subscriptions"
    # 所属企业（NULL = 平台级：平台超管账号、公共设备池、平台通用套餐）
    enterprise_id = db.Column(db.Integer, db.ForeignKey("enterprises.id"),
                              nullable=True, index=True)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plans.id"), nullable=False)
    status = db.Column(db.String(16), default="active")       # active/expired/cancelled
    started_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    # 提醒去重：记录已发过的提醒类型（expire_soon / expired / recharge_hint）
    reminded = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=_utcnow)
    # 这一笔订阅带来的云手机台数：同套餐重复购买时**累加台数**（不叠加时长）
    qty = db.Column(db.Integer, default=1)
    # 用户在「订阅记录」里删掉这条（软删除）：只从用户列表隐藏，管理员仍可见
    user_deleted = db.Column(db.Boolean, default=False)

    user = db.relationship("User")
    plan = db.relationship("Plan")

    def _used_devices(self) -> int:
        """该订阅下已经分配出去的设备数（按 sub_id 统计，失败返回 0）。"""
        try:
            from sqlalchemy import func
            from .extensions import db as _db
            return int(_db.session.query(func.count(Device.id))
                       .filter(Device.sub_id == self.id).scalar() or 0)
        except Exception:  # noqa: BLE001
            return 0

    def to_dict(self):
        now = _utcnow()
        remain_seconds = 0
        if self.expires_at:
            remain_seconds = max(0, int((self.expires_at - now).total_seconds()))
        return {
            "id": self.id,
            "user_id": self.user_id,
            "plan_id": self.plan_id,
            "plan_name": self.plan.name if self.plan else "",
            "model": self.plan.model if self.plan else "",
            "status": self.status,
            "started_at": iso_utc(self.started_at),
            "expires_at": iso_utc(self.expires_at),
            "remain_days": remain_seconds // 86400,
            # 精确剩余时长（前端显示"X 天 Y 小时 Z 分"用）
            "remain_seconds": remain_seconds,
            "remain_hours": remain_seconds // 3600,
            "remain_minutes": (remain_seconds % 3600) // 60,
            "qty": self.qty or 1,
            # 该订阅下已分配出去的云手机台数（前端显示"共 N 台 / 已分配 M 台"）
            "used_devices": self._used_devices(),
            "specs": (self.plan.to_dict().get("specs") if self.plan else {}) or {},
            "active": bool(self.status == "active" and self.expires_at and self.expires_at > now),
        }


class DiagnosticReport(db.Model):
    """诊断报告（系统/设备/批量），用于「诊断历史」。"""
    __tablename__ = "diagnostic_reports"
    id = db.Column(db.Integer, primary_key=True)
    scope = db.Column(db.String(16), default="system")     # system / device / all
    target_id = db.Column(db.Integer, nullable=True)       # 设备 id（scope=device 时）
    result = db.Column(db.Text, default="")                # JSON 报告
    summary = db.Column(db.String(255), default="")
    issues_found = db.Column(db.Integer, default=0)
    issues_fixed = db.Column(db.Integer, default=0)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)


class MaintenanceLog(db.Model):
    """设备维护日志（一键修复 / 批量修复的执行记录）。"""
    __tablename__ = "maintenance_logs"
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.Integer, db.ForeignKey("devices.id"), nullable=True)
    action = db.Column(db.String(64), default="")
    status = db.Column(db.String(16), default="success")   # success / failed
    detail = db.Column(db.Text, default="")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)


class Notification(db.Model):
    """用户站内提醒（到期前 3 天 / 到期当天 / 充值提示）。"""
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    ntype = db.Column(db.String(32), default="expire_soon")   # 见 NOTIFY_TYPES
    title = db.Column(db.String(128), default="")
    message = db.Column(db.Text, default="")
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    user = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "ntype": self.ntype,
            "title": self.title,
            "message": self.message,
            "is_read": self.is_read,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ---------------------------------------------------------------------------
# 云手机服务器（管理员自助添加，SSH 发现其中的云手机）
# ---------------------------------------------------------------------------
class Server(db.Model):
    """一台承载云手机的远程服务器（通过 SSH 管理/发现）。

    auth_type: password = 密码登录；key = 私钥文件登录（secret 存私钥路径）。
    说明：凭据与 .env 中的 SSH_PASSWORD 一样明文存储，接口返回时永不带出 secret。
    """
    __tablename__ = "servers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)           # 展示名（如 云手机-01）
    host = db.Column(db.String(128), nullable=False)          # IP 或域名
    port = db.Column(db.Integer, default=22, nullable=False)  # SSH 端口
    username = db.Column(db.String(64), default="root", nullable=False)
    auth_type = db.Column(db.String(16), default="password")  # password / key
    secret = db.Column(db.Text, default="")                   # 密码 或 私钥路径（key 模式）
    container_prefix = db.Column(db.String(64), default="redroid")  # 容器名前缀，用于发现
    adb_port_start = db.Column(db.Integer, default=5555)      # ADB 监听端口扫描起点
    adb_port_end = db.Column(db.Integer, default=5699)        # ADB 监听端口扫描终点
    status = db.Column(db.String(16), default="unknown")      # online / offline / unknown
    device_count = db.Column(db.Integer, default=0)           # 上次发现的云手机数量
    last_checked = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, default="")
    note = db.Column(db.String(255), default="")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    # 归属：NULL = 平台内置/共享（所有登录用户可见可用，如 117/122）；
    # 非空 = 某个用户自己登记的私有服务器（只有他本人可见、可用）。
    # 不能拿 created_by 当归属：平台内置那两台也是 admin（id=1）建的。
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    def to_dict(self, with_secret=False):
        d = {
            "id": self.id,
            "name": self.name,
            "host": self.host,
            "owner_id": self.owner_id,
            # shared=True 表示平台内置、所有人可用；前端据此决定能不能删
            "shared": self.owner_id is None,
            "port": self.port,
            "username": self.username,
            "auth_type": self.auth_type,
            "container_prefix": self.container_prefix,
            "adb_port_start": self.adb_port_start,
            "adb_port_end": self.adb_port_end,
            "status": self.status,
            "device_count": self.device_count or 0,
            "last_checked": self.last_checked.isoformat() if self.last_checked else None,
            "last_error": self.last_error or "",
            "note": self.note or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if with_secret:
            d["secret"] = self.secret or ""
        return d


# ---------------------------------------------------------------------------
# VPC 虚拟网络 + 出口线路（来自 new-vpc 分支合并）
# ---------------------------------------------------------------------------
class Vpc(db.Model):
    __tablename__ = "vpcs"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False, default="我的网络")
    cidr = db.Column(db.String(32), default="10.0.0.0/24")   # 网段
    status = db.Column(db.String(16), default="running")      # running / disabled
    egress_ip = db.Column(db.String(64), default="")          # 固定出口 IP（NAT 占位）
    security_level = db.Column(db.String(16), default="private") # open/private/secure 三档安全
    vpc_line_id = db.Column(db.Integer, db.ForeignKey("vpc_lines.id"), nullable=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    devices = db.relationship("VpcDevice", back_populates="vpc",
                              cascade="all, delete-orphan")
    vpc_line = db.relationship("VpcLine", foreign_keys=[vpc_line_id])

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "cidr": self.cidr,
            "status": self.status,
            "egress_ip": self.egress_ip,
            "vpc_line_id": self.vpc_line_id,
            "vpc_line_name": self.vpc_line.name if self.vpc_line else "",
            "owner_id": self.owner_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ---------------------------------------------------------------------------
# VPC 出口线路（SOCKS5，运营侧；一台客户网络最多指定一条）
# ---------------------------------------------------------------------------
VPC_LINE_STATUS = ["stopped", "running", "error"]


class VpcLine(db.Model):
    __tablename__ = "vpc_lines"
    __table_args__ = (
        db.UniqueConstraint("host", "port", name="uq_vpc_host_port"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    host = db.Column(db.String(128), nullable=False)
    port = db.Column(db.Integer, nullable=False)
    username = db.Column(db.String(128), default="")
    password = db.Column(db.String(256), default="")
    status = db.Column(db.String(16), default="stopped", nullable=False)
    last_test_ok = db.Column(db.Boolean, nullable=True)
    last_test_at = db.Column(db.DateTime, nullable=True)
    last_egress_ip = db.Column(db.String(64), default="")
    created_at = db.Column(db.DateTime, default=_utcnow)

    def to_dict(self, include_secret=False):
        data = {
            "id": self.id,
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "username": self.username or "",
            "has_password": bool(self.password),
            "status": self.status,
            "last_test_ok": self.last_test_ok,
            "last_test_at": iso_utc(self.last_test_at),
            "last_egress_ip": self.last_egress_ip or "",
            "created_at": iso_utc(self.created_at),
        }
        if include_secret:
            data["password"] = self.password or ""
        return data


class VpcDevice(db.Model):
    __tablename__ = "vpc_devices"

    id = db.Column(db.Integer, primary_key=True)
    vpc_id = db.Column(db.Integer, db.ForeignKey("vpcs.id"), nullable=False)
    device_id = db.Column(db.Integer, db.ForeignKey("devices.id"), nullable=False)
    ip = db.Column(db.String(32), default="")                 # VPC 内网 IP（10.0.0.x）
    created_at = db.Column(db.DateTime, default=_utcnow)

    vpc = db.relationship("Vpc", back_populates="devices")
    device = db.relationship("Device")


# ---------------------------------------------------------------------------
# 流媒体转发房间（客户端「流媒体 → 转发流」）
# ---------------------------------------------------------------------------
STREAM_STATUS = ["waiting", "live", "stopped"]


class ForwardStream(db.Model):
    """转发流房间。

    平台本身不做 WebRTC/RTMP 转发，媒体接入由外部媒体服务器（SRS / ZLMediaKit）承担；
    本表登记房间，并在配置了 STREAM_MEDIA_API 时把媒体服务器的真实状态
    （在线 / 码率 / 来源 IP）同步进来，见 app/forward_stream.py。
    """
    __tablename__ = "forward_streams"

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    status = db.Column(db.String(16), nullable=False, default="waiting")  # waiting/live/stopped
    source_ip = db.Column(db.String(64), default="")
    bitrate = db.Column(db.Integer, default=0)          # bps
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    stopped_at = db.Column(db.DateTime, nullable=True)


# ---------------------------------------------------------------------------
# 云手机系统镜像（服务器上的一张 Docker 镜像）
# ---------------------------------------------------------------------------
IMAGE_STATUS = ["creating", "ready", "error"]


class SystemImage(db.Model):
    """云手机系统镜像。

    镜像实体是**某台云手机服务器上的一张 Docker 镜像**，本表只存元数据：
    镜像在哪台服务器（server_id / server_host）、仓库名与 tag（repo:tag）。
    上传型镜像额外在平台侧留一份源文件（file_path），便于重新分发。

    不做任何假数据：status=error 时 error 里写清楚失败原因，前端如实展示。
    """
    __tablename__ = "system_images"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    version = db.Column(db.String(32), default="12.0")
    desc = db.Column(db.Text, default="")
    # kind: docker = 容器镜像（docker commit）；device = 真机分区镜像（dd 分区）
    kind = db.Column(db.String(16), default="docker", nullable=False)
    # 真机镜像：包含哪些分区（JSON 数组，如 ["super","boot","vbmeta","dtbo"]）
    partitions = db.Column(db.Text, default="")
    # 真机镜像：来源板子的 adb serial（便于追溯）
    source_serial = db.Column(db.String(64), default="")
    repo = db.Column(db.String(160), default="")            # docker 仓库名
    tag = db.Column(db.String(64), default="latest")
    server_id = db.Column(db.Integer, db.ForeignKey("servers.id"), nullable=True)
    server_host = db.Column(db.String(128), default="")     # 冗余，便于展示与校验
    size_bytes = db.Column(db.BigInteger, default=0)
    builtin = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(16), default="ready")      # creating/ready/error
    error = db.Column(db.Text, default="")
    source_device_id = db.Column(db.Integer, db.ForeignKey("devices.id"), nullable=True)
    file_path = db.Column(db.String(512), default="")       # 上传型：平台侧源文件
    file_name = db.Column(db.String(255), default="")
    checksum = db.Column(db.String(80), default="")         # sha256（上传型）
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    def ref(self) -> str:
        """docker 镜像引用 repo:tag。"""
        if not self.repo:
            return ""
        return f"{self.repo}:{self.tag or 'latest'}"

    def size_text(self) -> str:
        n = float(self.size_bytes or 0)
        if n <= 0:
            return "未知"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return ("%d%s" % (n, unit)) if unit == "B" else ("%.1f%s" % (n, unit))
            n /= 1024.0
        return "未知"

    @property
    def partition_list(self) -> list:
        if not self.partitions:
            return []
        try:
            v = json.loads(self.partitions)
            return v if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version or "",
            "desc": self.desc or "",
            "kind": self.kind or "docker",
            "partitions": self.partition_list,
            "source_serial": self.source_serial or "",
            "repo": self.repo or "",
            "tag": self.tag or "latest",
            "ref": self.ref(),
            "server_id": self.server_id,
            "server_host": self.server_host or "",
            "size_bytes": int(self.size_bytes or 0),
            "size": self.size_text(),
            "builtin": bool(self.builtin),
            "status": self.status or "ready",
            "error": self.error or "",
            "source_device_id": self.source_device_id,
            "file_name": self.file_name or "",
            "has_file": bool(self.file_path and os.path.isfile(self.file_path)),
            "owner_id": self.owner_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ---------------------------------------------------------------------------
# 退款申请（阶段2）：用户对"未使用过云手机"的订阅发起，管理台处理
# ---------------------------------------------------------------------------
class RefundRequest(db.Model):
    __tablename__ = "refund_requests"
    # 所属企业（NULL = 平台级：平台超管账号、公共设备池、平台通用套餐）
    enterprise_id = db.Column(db.Integer, db.ForeignKey("enterprises.id"),
                              nullable=True, index=True)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey("subscriptions.id"), nullable=False)
    # 来源订单（规则：只有"付款生效的套餐订单"才能申请退款）
    order_id = db.Column(db.Integer, nullable=True)
    plan_name = db.Column(db.String(64), default="")
    amount_cents = db.Column(db.Integer, default=0)
    reason = db.Column(db.String(255), default="")
    status = db.Column(db.String(16), default="pending")      # pending/approved/rejected
    admin_note = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=_utcnow)
    handled_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "subscription_id": self.subscription_id,
            "order_id": self.order_id,
            "plan_name": self.plan_name,
            "amount_cents": self.amount_cents or 0,
            "amount_yuan": round((self.amount_cents or 0) / 100, 2),
            "reason": self.reason or "",
            "status": self.status,
            "admin_note": self.admin_note or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "handled_at": self.handled_at.isoformat() if self.handled_at else None,
        }


# ---------------------------------------------------------------------------
# 统一门户接入（product_code=phone）
# ---------------------------------------------------------------------------
class PortalUserMap(db.Model):
    """门户用户 UUID ↔ 本地影子用户（只按 UUID 匹配，永不按用户名）。"""
    __tablename__ = "portal_user_map"

    portal_user_id = db.Column(db.String(64), primary_key=True)
    local_user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                              nullable=False, unique=True)
    created_at = db.Column(db.DateTime, default=_utcnow)


class PortalProvision(db.Model):
    """门户开通记录（幂等键 order_item_id，string）。"""
    __tablename__ = "portal_provisions"

    id = db.Column(db.Integer, primary_key=True)
    order_item_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    portal_user_id = db.Column(db.String(64), nullable=False, index=True)
    sku_id = db.Column(db.String(64), nullable=False, index=True)
    quantity = db.Column(db.Integer, default=1)
    period_hours = db.Column(db.Integer, default=720)
    status = db.Column(db.String(16), default="pending")   # pending/granting/success/failed
    instance_id = db.Column(db.String(64), default="")
    device_ids = db.Column(db.Text, default="[]")          # JSON 数组（本地 Device.id）
    expire_at = db.Column(db.DateTime, nullable=True)
    error_code = db.Column(db.Integer, nullable=True)
    error_msg = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)


class ProvisionJob(db.Model):
    """开通后的后台任务（设备初始化 / 装 APK / 套桌面模板）。

    portal_provision 开通成功后入队；scheduler 每 5 秒取一批执行。
    失败按指数退避重试，超过 max_attempts 进死信（status=dead）。
    """
    __tablename__ = "provision_jobs"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(40), nullable=False)
    order_item_id = db.Column(db.String(64), index=True)
    payload = db.Column(db.Text, default="{}")
    status = db.Column(db.String(16), default="pending", index=True)
    attempts = db.Column(db.Integer, default=0)
    max_attempts = db.Column(db.Integer, default=5)
    next_run_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    last_error = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "kind": self.kind, "order_item_id": self.order_item_id,
            "status": self.status, "attempts": self.attempts,
            "max_attempts": self.max_attempts, "last_error": self.last_error,
            "payload": self.payload,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
