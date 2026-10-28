"""蓝图集合。"""
from .auth import auth_bp
from .users import users_bp
from .devices import devices_bp
from .tasks import tasks_bp
from .dashboard import dashboard_bp
from .audit import audit_bp
from .files import files_bp
from .scripts import scripts_bp
from .groups import groups_bp
from .alerts import alerts_bp
from .exports import exports_bp
from .settings import settings_bp
from .images import images_bp
from .ssh_bridge import bp as ssh_bridge_bp
from .billing import billing_bp
from .servers import servers_bp
from .vpc import vpc_bp
from .vpc_lines import vpc_lines_bp
from .streams import streams_bp
from .internal import internal_bp
from .org import org_bp
from .enterprises import enterprises_bp

__all__ = [
    "auth_bp",
    "users_bp",
    "devices_bp",
    "tasks_bp",
    "dashboard_bp",
    "audit_bp",
    "files_bp",
    "scripts_bp",
    "groups_bp",
    "alerts_bp",
    "exports_bp",
    "settings_bp",
    "images_bp",
    "ssh_bridge_bp",
    "billing_bp",
    "servers_bp",
    "vpc_bp",
    "vpc_lines_bp",
    "streams_bp",
    "internal_bp",
    "org_bp",
    "enterprises_bp",
]
