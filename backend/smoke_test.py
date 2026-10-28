"""冒烟测试：用 Flask 测试客户端验证三大任务块。
运行：.venv/Scripts/python.exe smoke_test.py
"""
import os
import tempfile

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/smoke.db"
os.environ["JWT_SECRET"] = "test-secret"

from app import create_app
from app.extensions import db
from app.models import User

app = create_app()
client = app.test_client()


def test_boot_and_seed():
    # 启动即建表 + seed（admin/root 已建）
    with app.app_context():
        assert db.session.query(User).filter_by(username="admin").first() is not None
        assert db.session.query(User).filter_by(username="root").first() is not None


def test_login_jwt():
    r = client.post("/api/auth/login", json={"username": "admin", "password": "Admin@123456"})
    assert r.status_code == 200, r.get_json()
    tok = r.get_json()["access_token"]
    assert tok
    # /auth/me 带 token
    r2 = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert r2.status_code == 200
    assert r2.get_json()["role"] == "admin"
    return tok


def test_rbac_blocks_viewer():
    # 新建 viewer，其访问 /api/users 应 403
    client.post("/api/auth/login", json={"username": "admin", "password": "Admin@123456"})
    adm = client.post("/api/auth/login", json={"username": "admin", "password": "Admin@123456"}).get_json()["access_token"]
    client.post("/api/users", headers={"Authorization": f"Bearer {adm}"},
                json={"username": "viewer1", "password": "Viewer@123", "role": "viewer"})
    vtok = client.post("/api/auth/login", json={"username": "viewer1", "password": "Viewer@123"}).get_json()["access_token"]
    r = client.get("/api/users", headers={"Authorization": f"Bearer {vtok}"})
    assert r.status_code == 403, r.get_json()  # viewer 无权看用户管理


def test_device_and_dashboard(tok):
    r = client.post("/api/devices", headers={"Authorization": f"Bearer {tok}"},
                    json={"name": "phone-1", "backend": "simulator"})
    assert r.status_code == 201, r.get_json()
    did = r.get_json()["id"]
    # 批量
    r2 = client.post("/api/devices/batch", headers={"Authorization": f"Bearer {tok}"},
                     json={"count": 3, "prefix": "batch"})
    assert r2.status_code == 201 and len(r2.get_json()) == 3
    # 看板
    r3 = client.get("/api/metrics/overview", headers={"Authorization": f"Bearer {tok}"})
    assert r3.status_code == 200
    assert r3.get_json()["kpis"]["total_devices"] >= 4


def test_task_execution(tok):
    r = client.post("/api/tasks", headers={"Authorization": f"Bearer {tok}"},
                    json={"name": "t1", "action": "open_url", "schedule_type": "once", "device_ids": []})
    assert r.status_code == 201, r.get_json()
    tid = r.get_json()["id"]
    r2 = client.post(f"/api/tasks/{tid}/run", headers={"Authorization": f"Bearer {tok}"})
    assert r2.status_code == 200, r2.get_json()


if __name__ == "__main__":
    test_boot_and_seed()
    tok = test_login_jwt()
    test_rbac_blocks_viewer()
    test_device_and_dashboard(tok)
    test_task_execution(tok)
    print("ALL SMOKE TESTS PASSED ✅")
