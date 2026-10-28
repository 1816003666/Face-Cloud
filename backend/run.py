"""入口：python run.py 启动开发服务器。"""
import os
from app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8001"))
    # debug=False：Werkzeug reloader 会 fork 多进程，多进程并发写 SQLite 导致
# "database is locked"，且多次重启会累积孤儿进程。单进程运行最稳。
app.run(host="0.0.0.0", port=port, debug=False)
