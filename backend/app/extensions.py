"""全局扩展对象，避免循环依赖。"""
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

db = SQLAlchemy()
cors = CORS()

# SQLite 并发写锁修复：启用 WAL + busy_timeout。
# 多线程（WebSocket 广播、WebRTC 推流、APScheduler、HTTP 控制）同时写库时，
# 默认 rollback journal 会频繁报 "database is locked"。
try:
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    @event.listens_for(Engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        try:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            # busy_timeout 原来只有 10 秒。实测（122 生产库）取写锁**最慢要等 8 秒**
            # —— 也就是常态就在 10 秒边缘，稍一拥堵就报
            #   sqlite3.OperationalError: database is locked
            # 各种写接口随机 500（制作镜像、设备同步、metrics 落库都中过招）。
            # SQLite 一次只允许一个写者，等待比直接失败合理，所以放宽到 60 秒。
            cursor.execute("PRAGMA busy_timeout=60000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()
        except Exception:
            pass
except Exception:
    pass
