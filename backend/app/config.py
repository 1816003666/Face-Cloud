"""应用配置。

读取 .env（python-dotenv）。生产环境务必覆盖下面带 * 的默认值。
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ---- 基础 ----
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    APP_NAME = os.environ.get("APP_NAME", "CloudPhoneBoard")

    # ---- 数据库 ----
    # 默认用 SQLite，便于本地免 Docker 直接跑；生产改 Postgres 连接串
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///cloud_phone.db"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ---- JWT *（生产必须改）----
    JWT_SECRET = os.environ.get("JWT_SECRET", "change-me-in-production-please")
    JWT_EXPIRE_HOURS = int(os.environ.get("JWT_EXPIRE_HOURS", "168"))

    # 开通后的后台初始化（装 APK / 套桌面模板）；空表示跳过
    PROVISION_INIT_APK = os.environ.get("PROVISION_INIT_APK", "")
    PROVISION_INIT_TEMPLATE_DIR = os.environ.get("PROVISION_INIT_TEMPLATE_DIR", "")

    # ---- 设备编排后端 ----
    # simulator = 纯内存 Mock，免 Docker；redroid = 真 Docker Android 容器
    DEVICE_BACKEND = os.environ.get("DEVICE_BACKEND", "simulator")

    # ---- 云手机服务器 ----
    # 承载 redroid 容器 / ADB 设备的物理服务器地址，设备出口 IP 与 ADB serial 均基于此生成
    CLOUD_PHONE_SERVER = os.environ.get("CLOUD_PHONE_SERVER", "127.0.0.1")
    # ADB 起始端口，每台设备递增 1（5555, 5557, ...）
    CLOUD_PHONE_ADB_START_PORT = int(os.environ.get("CLOUD_PHONE_ADB_START_PORT", "5555"))

    # ---- ADB（真实设备操控） ----
    # adb 可执行文件路径；留空则尝试从 PATH 中查找
    ADB_PATH = os.environ.get("ADB_PATH", "")
    # 真实设备截图缓存目录
    SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", "instance/screenshots")

    # ---- SSH（远程管理 redroid Docker 容器） ----
    # 云手机服务器 SSH 连接信息，用于创建/删除 Docker 容器
    SSH_HOST = os.environ.get("SSH_HOST", "")
    SSH_PORT = int(os.environ.get("SSH_PORT", "22"))
    SSH_USERNAME = os.environ.get("SSH_USERNAME", "")
    SSH_PASSWORD = os.environ.get("SSH_PASSWORD", "")
    SSH_KEY_FILE = os.environ.get("SSH_KEY_FILE", "")
    # redroid Docker 镜像
    REDROID_IMAGE = os.environ.get("REDROID_IMAGE", "redroid/redroid:12.0.0_latest")
    # redroid 容器名前缀
    REDROID_CONTAINER_PREFIX = os.environ.get("REDROID_CONTAINER_PREFIX", "redroid")

    # ---- 云手机系统镜像 ----
    # 上传型镜像在平台侧的落盘目录
    IMAGE_STORE_DIR = os.environ.get("IMAGE_STORE_DIR", "instance/images")
    # 自建镜像的 docker 仓库名前缀（commit 出来的镜像形如 cpimg-<slug>-<id>:<tag>）
    IMAGE_REPO_PREFIX = os.environ.get("IMAGE_REPO_PREFIX", "cpimg")
    # 单次 docker 操作超时（秒）。commit/run 可能很慢，默认 30 分钟
    IMAGE_DOCKER_TIMEOUT = int(os.environ.get("IMAGE_DOCKER_TIMEOUT", "1800"))
    # 允许「重建目标设备」这种破坏性部署（默认关闭，需显式打开）
    IMAGE_DEPLOY_REPLACE_ENABLED = os.environ.get(
        "IMAGE_DEPLOY_REPLACE_ENABLED", "false").lower() == "true"

    # ---- 真机（板子）分区镜像 ----
    # 板子的 SSH 地址清单（逗号分隔）；真机镜像只能对这些地址操作
    IMAGE_BOARD_HOSTS = os.environ.get("IMAGE_BOARD_HOSTS", "")
    IMAGE_BOARD_USER = os.environ.get("IMAGE_BOARD_USER", "root")
    IMAGE_BOARD_KEY = os.environ.get("IMAGE_BOARD_KEY", "")
    IMAGE_BOARD_PORT = int(os.environ.get("IMAGE_BOARD_PORT", "22"))

    # ---- 视频投屏（scrcpy server 自动部署）----
    # 服务器上 scrcpy-server.jar 路径：新容器创建/启动后自动 push 并拉起 server，
    # 保证每台云手机首次预览即有全新编码器、画面清晰一致
    SCRCPY_SERVER_JAR = os.environ.get(
        "SCRCPY_SERVER_JAR",
        "/home/zzzz/ws-scrcpy/dist/vendor/Genymobile/scrcpy/scrcpy-server.jar",
    )
    # scrcpy server 版本标识（需与 ws-scrcpy 版本匹配）
    SCRCPY_SERVER_VERSION = os.environ.get("SCRCPY_SERVER_VERSION", "1.19-ws8")
    # scrcpy server 视频监听端口（容器内，与 ws-scrcpy remote=tcp:8886 对应）
    SCRCPY_SERVER_PORT = int(os.environ.get("SCRCPY_SERVER_PORT", "8886"))

    # ---- redroid 容器模板（以第二台云手机 redroid_2/5557 为基准：720P 流畅优先 + Pixel 6 伪装）----
    REDROID_GPU_MODE = os.environ.get("REDROID_GPU_MODE", "guest")
    REDROID_WIDTH = os.environ.get("REDROID_WIDTH", "720")
    REDROID_HEIGHT = os.environ.get("REDROID_HEIGHT", "1280")
    REDROID_DPI = os.environ.get("REDROID_DPI", "320")
    # 第二台未设 fps（走默认）；留空则创建时不生成该参数
    REDROID_FPS = os.environ.get("REDROID_FPS", "")
    REDROID_NATIVE_BRIDGE = os.environ.get("REDROID_NATIVE_BRIDGE", "")
    # 机型伪装（对齐第二台 Pixel 6）；serialno 由端口自动生成保证每台唯一
    REDROID_PRODUCT_BRAND = os.environ.get("REDROID_PRODUCT_BRAND", "google")
    REDROID_PRODUCT_MANUFACTURER = os.environ.get("REDROID_PRODUCT_MANUFACTURER", "Google")
    REDROID_PRODUCT_MODEL = os.environ.get("REDROID_PRODUCT_MODEL", "Pixel 6")
    REDROID_PRODUCT_NAME = os.environ.get("REDROID_PRODUCT_NAME", "Pixel 6")
    REDROID_LOCALE = os.environ.get("REDROID_LOCALE", "zh-CN")

    # ---- 新容器初始化（默认应用：iLauncher 启动器 / x浏览器 默认浏览器 / 隐藏 WebViewShell）----
    REDROID_SETUP_ENABLED = os.environ.get("REDROID_SETUP_ENABLED", "true").lower() in ("1", "true", "yes")
    DEFAULT_LAUNCHER_PKG = os.environ.get("DEFAULT_LAUNCHER_PKG", "com.launcheros15.ilauncher")
    DEFAULT_BROWSER_PKG = os.environ.get("DEFAULT_BROWSER_PKG", "com.mmbox.xbrowser")
    HIDE_WEBVIEW_SHELL_PKG = os.environ.get("HIDE_WEBVIEW_SHELL_PKG", "org.chromium.webview_shell")

    # ---- CORS ----
    CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*").split(",")

    # ---- 文件上传目录（文件管理模块用）----
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "uploads")

    # ---- 初始管理员（首次启动 seed 用）----
    SEED_ADMIN_USERNAME = os.environ.get("SEED_ADMIN_USERNAME", "admin")
    SEED_ADMIN_PASSWORD = os.environ.get("SEED_ADMIN_PASSWORD", "Admin@123456")
    SEED_SUPERADMIN_USERNAME = os.environ.get("SEED_SUPERADMIN_USERNAME", "root")
    SEED_SUPERADMIN_PASSWORD = os.environ.get("SEED_SUPERADMIN_PASSWORD", "Root@123456")

    # ---- 流媒体（转发流 / PC 摄像头虚拟推流）----
    # 对外可用的媒体服务器主机（生成 WebRTC/RTMP 推流地址用）；留空则用请求 Host
    STREAM_PUBLIC_HOST = os.environ.get("STREAM_PUBLIC_HOST", "")
    # 媒体服务器 RTMP 端口 / WebRTC 路径前缀
    STREAM_RTMP_PORT = int(os.environ.get("STREAM_RTMP_PORT", "1935"))
    STREAM_WEBRTC_PREFIX = os.environ.get("STREAM_WEBRTC_PREFIX", "live")
    # 推流/播放基地址精确覆盖（留空则按请求 Host 推导）。
    # 例（MediaMTX）：STREAM_WEBRTC_BASE=http://112.4.135.254:8889/live（WHIP）
    #                STREAM_RTMP_BASE=rtmp://112.4.135.254:1935/live
    #                STREAM_HLS_BASE=http://112.4.135.254:8888/live
    STREAM_WEBRTC_BASE = os.environ.get("STREAM_WEBRTC_BASE", "")
    # 推流账号（媒体服务器开了 publish 鉴权时必须配；会拼进 /api/streams/config 返回的推流地址）
    # 播放页对外基地址（云手机浏览器要能访问，例如 http://112.4.135.254:8003）；
    # 留空则按调用方请求的 Host 推导
    STREAM_PLAYER_BASE = os.environ.get("STREAM_PLAYER_BASE", "")
    STREAM_PUBLISH_USER = os.environ.get("STREAM_PUBLISH_USER", "")
    STREAM_PUBLISH_PASS = os.environ.get("STREAM_PUBLISH_PASS", "")
    STREAM_RTMP_BASE = os.environ.get("STREAM_RTMP_BASE", "")
    STREAM_HLS_BASE = os.environ.get("STREAM_HLS_BASE", "")
    # 媒体服务器 HTTP API 根地址（MediaMTX: http://127.0.0.1:9997；SRS: http://host:1985；ZLMediaKit: http://host:80）。
    # 配置后 /api/streams 会读取真实流状态（在线/码率/来源 IP）；留空则房间只登记，
    # 状态保持「等待推流」。
    STREAM_MEDIA_API = os.environ.get("STREAM_MEDIA_API", "")
    # 云手机侧播放器包名（装了自带软解的播放器时优先用它打开画面，比浏览器可靠）。
    # 例：VIRTUAL_CAMERA_PLAYER_PKG=org.videolan.vlc（VLC for Android）
    VIRTUAL_CAMERA_PLAYER_PKG = os.environ.get("VIRTUAL_CAMERA_PLAYER_PKG", "")
    VIRTUAL_CAMERA_PLAYER_ACTIVITY = os.environ.get(
        "VIRTUAL_CAMERA_PLAYER_ACTIVITY", "org.videolan.vlc/.StartActivity")
    # 云手机虚拟摄像头管线命令模板（在云手机服务器上执行，{url}/{serial}/{device_id}/{ip} 占位符）。
    # 例如：ffmpeg -re -i {url} -f v4l2 /dev/video0
    # 未配置时 /api/devices/<id>/virtual-camera-stream 只登记地址并返回 501，
    # 不假装已下发到设备（云手机侧需要 v4l2loopback 之类的虚拟摄像头管线）。
    VIRTUAL_CAMERA_CMD = os.environ.get("VIRTUAL_CAMERA_CMD", "")
    VIRTUAL_CAMERA_STOP_CMD = os.environ.get("VIRTUAL_CAMERA_STOP_CMD", "")

    # ---- 告警系统阈值（任务书 #12）----
    ALERT_OFFLINE_SECONDS = int(os.environ.get("ALERT_OFFLINE_SECONDS", "120"))
    ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")
    # 告警/巡检相关阈值
    ALERT_CPU_LIMIT = float(os.environ.get("ALERT_CPU_LIMIT", "90"))
    ALERT_MEM_LIMIT = float(os.environ.get("ALERT_MEM_LIMIT", "90"))

    # ---- 统一门户接入（产品侧）----
    # 门户内网地址（我们调门户用），如 http://<门户IP>:3000
    PORTAL_BASE_URL = os.environ.get("PORTAL_BASE_URL", "")
    # 代理登录：调门户的登录接口路径与超时
    PORTAL_LOGIN_PATH = os.environ.get("PORTAL_LOGIN_PATH", "/api/auth/login")
    PORTAL_LOGIN_TIMEOUT_MS = int(os.environ.get("PORTAL_LOGIN_TIMEOUT_MS", "5000"))
    # 待支付订单超过 N 分钟自动取消
    ORDER_PENDING_TIMEOUT_MIN = int(os.environ.get("ORDER_PENDING_TIMEOUT_MIN", "30"))
    PORTAL_ENABLED = os.environ.get("PORTAL_ENABLED", "true").lower() in ("1", "true", "yes")
    PORTAL_PRODUCT_CODE = os.environ.get("PORTAL_PRODUCT_CODE", "phone")
    # 门户下发的内网密钥（须与门户 PRODUCT_PHONE_INTERNAL_KEY 一致）；未配置时内网接口一律拒绝
    PORTAL_INTERNAL_KEY = os.environ.get("PORTAL_INTERNAL_KEY", "")
    # 允许调 /api/internal/* 的来源 IP（逗号分隔，取 X-Real-IP/X-Forwarded-For）；留空=不限制
    PORTAL_ALLOWED_IPS = os.environ.get("PORTAL_ALLOWED_IPS", "")
    # 门户可售库存上限（云手机真实可用台数）
    PORTAL_STOCK_LIMIT = int(os.environ.get("PORTAL_STOCK_LIMIT", "60"))
    # 影子用户（门户用户在本地建号的规则）
    PORTAL_SHADOW_PREFIX = os.environ.get("PORTAL_SHADOW_PREFIX", "portal_")
    PORTAL_SHADOW_PASSWORD_HASH = os.environ.get(
        "PORTAL_SHADOW_PASSWORD_HASH", "!portal-shadow-no-local-login")
    PORTAL_SHADOW_ROLE = os.environ.get("PORTAL_SHADOW_ROLE", "operator")
    # [已废弃] 曾经用它统一覆盖「每单元几台云手机」，会一次造成 10 倍价格误差。
    # 现在台数只从套餐 specs.device_count 取，此值仅为兼容旧 .env 保留，不再被读取。
    PORTAL_DEVICES_PER_UNIT = int(os.environ.get("PORTAL_DEVICES_PER_UNIT", "0"))
    # 单次最多买几份。原来 3 也是"自动退款"的来源之一（买 4 份直接被拒）。
    PORTAL_MAX_QTY_PER_ORDER = int(os.environ.get("PORTAL_MAX_QTY_PER_ORDER", "20"))
    # 单次开通最多几台。原默认 10 与「包季/旗舰 每份 10 台」直接打架：
    # 买 2 份就是 20 台，会被判「台数超限」→ 门户侧只能自动退款。
    PORTAL_MAX_DEVICES_PER_PROVISION = int(
        os.environ.get("PORTAL_MAX_DEVICES_PER_PROVISION", "200"))
    # 影子用户默认角色（本平台"普通用户"档）
    PORTAL_RECONCIL_ENABLED = os.environ.get(
        "PORTAL_RECONCIL_ENABLED", "false").lower() in ("1", "true", "yes")
