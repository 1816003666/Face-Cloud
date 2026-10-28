# -*- coding: utf-8 -*-
"""镜像长任务进度登记表（进程内）。

背景：做镜像 / 刷机要十几分钟，原来是"一个 HTTP 请求跑到底"：
  * 前端只能干等，进度条只能做成不定态；
  * nginx / 客户端任何一环超时，用户就以为失败了（其实后端还在跑）；
  * 客户端关掉再打开，进度就丢了。

现在改成：提交 → **立刻返回 task_id** → 后台线程干活 → 前端轮询
`GET /images/tasks/<id>` 拿真实的 stage / done / total / percent。

状态放进程内（单进程 run_simple，够用）：
  * 进程重启会丢任务，但镜像行上的 status/error 还在库里，可以兜底；
  * 只保留最近 _MAX 条，避免无限增长。
"""
from __future__ import annotations

import threading
import time
import uuid

_LOCK = threading.Lock()
_TASKS: dict = {}
_MAX = 60


def _now() -> float:
    return time.time()


def create(kind: str, title: str, target: str, owner_id=None, total: int = 0) -> str:
    """登记一个新任务，返回 task_id。"""
    tid = uuid.uuid4().hex[:12]
    with _LOCK:
        _TASKS[tid] = {
            "id": tid,
            "kind": kind,              # make_device / deploy_device / make_container ...
            "title": title,
            "target": target,
            "owner_id": owner_id,
            "status": "running",       # running / ok / fail
            "stage": "准备中",
            "done": 0,
            "total": int(total or 0),
            "percent": 0,
            "message": "",
            "error": "",
            "image_id": None,
            "started_at": _now(),
            "finished_at": None,
        }
        if len(_TASKS) > _MAX:
            old = sorted(_TASKS, key=lambda k: _TASKS[k]["started_at"])[: len(_TASKS) - _MAX]
            for k in old:
                _TASKS.pop(k, None)
    return tid


def step(tid: str, stage=None, done=None, total=None, message=None, image_id=None) -> None:
    """更新进度。done/total 是"第几步/共几步"——任务本身知道粒度。"""
    with _LOCK:
        t = _TASKS.get(tid)
        if t is None:
            return
        if stage is not None:
            t["stage"] = stage
        if done is not None:
            t["done"] = int(done)
        if total is not None:
            t["total"] = int(total)
        if message is not None:
            t["message"] = message
        if image_id is not None:
            t["image_id"] = image_id
        tot = t["total"] or 0
        if tot:
            t["percent"] = int(min(100, max(0, round(t["done"] * 100.0 / tot))))


def finish(tid: str, ok: bool, message: str = "", error: str = "", image_id=None) -> None:
    with _LOCK:
        t = _TASKS.get(tid)
        if t is None:
            return
        t["status"] = "ok" if ok else "fail"
        t["stage"] = "完成" if ok else "失败"
        if ok:
            t["percent"] = 100
            t["done"] = t["total"] or t["done"]
        if message:
            t["message"] = message
        if error:
            t["error"] = str(error)[:300]
        if image_id is not None:
            t["image_id"] = image_id
        t["finished_at"] = _now()


def _snapshot(t: dict) -> dict:
    d = dict(t)
    d["elapsed"] = int((d["finished_at"] or _now()) - d["started_at"])
    return d


def get(tid: str, owner_id=None):
    with _LOCK:
        t = _TASKS.get(tid)
        if t is None:
            return None
        if owner_id is not None and t.get("owner_id") not in (None, owner_id):
            return None
        return _snapshot(t)


def list_for(owner_id=None, limit: int = 20) -> list:
    """按开始时间倒序；owner_id 给了就只返回该用户的（None 视为公共）。"""
    with _LOCK:
        rows = [_snapshot(t) for t in _TASKS.values()
                if owner_id is None or t.get("owner_id") in (None, owner_id)]
    rows.sort(key=lambda x: x["started_at"], reverse=True)
    return rows[:limit]
