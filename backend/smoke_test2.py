"""冒烟测试：验证石文盛名下新增模块（注册/文件/脚本/分组/审计）。"""
import io
import os
import tempfile

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/smoke2.db"
os.environ["DEVICE_BACKEND"] = "simulator"

from app import create_app
from app.extensions import db
from app.models import Device, Group

app = create_app()
c = app.test_client()


def j(r):
    return r.get_json()


# 1) seed 后登录 admin
r = c.post("/api/auth/login", json={"username": "admin", "password": "Admin@123456"})
assert r.status_code == 200, r.get_data(as_text=True)
token = j(r)["access_token"]
H = {"Authorization": f"Bearer {token}"}
print("[1] admin 登录 OK")

# 2) 注册新用户（viewer）
r = c.post("/api/auth/register", json={"username": "alice", "password": "abc123"})
assert r.status_code == 201, r.get_data(as_text=True)
print("[2] 注册新用户 OK ->", j(r)["user"]["username"], j(r)["user"]["role"])

# 准备一台设备（供脚本/分组操作）
with app.app_context():
    g = Group(name="g1", owner_id=1)
    db.session.add(g)
    db.session.commit()
    d = Device(name="dev1", group_id=g.id, status="running", backend="simulator")
    db.session.add(d)
    db.session.commit()
    DID = d.id
print("[prepare] 建好设备 dev1 id=", DID)

# 3) 上传文件
data = {"file": (io.BytesIO(b"hello cloud phone"), "hello.txt")}
r = c.post("/api/files/upload", headers=H, data=data,
           content_type="multipart/form-data")
assert r.status_code == 201, r.get_data(as_text=True)
fid = j(r)["id"]
print("[3] 上传文件 OK ->", j(r)["filename"], fid)

# 4) 列文件
r = c.get("/api/files", headers=H)
assert r.status_code == 200 and j(r)["total"] >= 1
print("[4] 列文件 OK total=", j(r)["total"])

# 5) 创建脚本
r = c.post("/api/scripts", headers=H,
           json={"name": "s1", "steps": [{"action": "open_url", "params": {"url": "x"}}]})
assert r.status_code == 201, r.get_data(as_text=True)
sid = j(r)["id"]
print("[5] 创建脚本 OK ->", sid)

# 6) 执行脚本
r = c.post(f"/api/scripts/{sid}/execute", headers=H, json={"device_ids": [DID]})
assert r.status_code == 200 and j(r)["ok"] == 1
print("[6] 执行脚本 OK ->", j(r))

# 7) 创建分组 + 批量操作（设备需属于该分组）
r = c.post("/api/groups", headers=H, json={"name": "g2", "description": "test"})
assert r.status_code == 201, r.get_data(as_text=True)
gid = j(r)["id"]
with app.app_context():
    d2 = Device(name="dev2", group_id=gid, status="stopped", backend="simulator")
    db.session.add(d2)
    db.session.commit()
    DID2 = d2.id
r = c.post(f"/api/groups/{gid}/batch-action", headers=H,
           json={"action": "start", "device_ids": [DID2]})
assert r.status_code == 200 and j(r)["ok"] == 1, j(r)
print("[7] 分组批量开机 OK ->", j(r))

# 8) 审计统计
r = c.get("/api/audit/stats", headers=H)
assert r.status_code == 200
print("[8] 审计统计 OK ->", j(r)[:3], "...")

# 9) RBAC：viewer 不能上传文件
r = c.post("/api/auth/login", json={"username": "alice", "password": "abc123"})
t2 = j(r)["access_token"]
r = c.post("/api/files/upload", headers={"Authorization": f"Bearer {t2}"},
           data={"file": (io.BytesIO(b"x"), "x.txt")},
           content_type="multipart/form-data")
assert r.status_code == 403, r.get_data(as_text=True)
print("[9] RBAC 拦截 viewer 上传 OK (403)")

print("\nALL NEW MODULES SMOKE TEST PASSED ✅")
