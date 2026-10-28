# -*- coding: utf-8 -*-
"""出口 IP：给云手机绑定一条 SOCKS5 出口线路（一台一号 IP，避免被风控按 IP 聚类）。

复用已有的 VPC 能力：`vpc_lines` 是出口线路，`orchestrator.apply_vpc_sidecar()`
为某台 redroid 容器起 sidecar 把流量导到该 SOCKS5；绑定成功后把真实出口 IP
写进 `devices.egress_ip`。
"""
from __future__ import annotations

from .extensions import db
from .models import Device, VpcLine


def running_lines() -> list:
    return (db.session.query(VpcLine)
            .filter(VpcLine.status == "running")
            .order_by(VpcLine.id).all())


def _line_load(line_id: int) -> int:
    """这条线现在挂了多少台设备（尽量摊平）。"""
    return db.session.query(Device).filter(Device.egress_line_id == line_id).count()


def pick_line() -> VpcLine | None:
    """挑负载最小的可用线路。"""
    lines = running_lines()
    if not lines:
        return None
    return min(lines, key=lambda ln: _line_load(ln.id))


def bind(device: Device, line: VpcLine | None = None) -> dict:
    """给设备绑一条出口线路，返回 {ok, egress_ip, error}。"""
    from .routes.vpc import _apply_line, _device_port

    line = line or pick_line()
    if line is None:
        return {"ok": False, "error": "没有可用的出口线路"}
    if _device_port(device) is None:
        return {"ok": False, "error": "只支持 redroid 设备"}
    err = _apply_line(device, line)
    if err:
        return {"ok": False, "error": err}
    device.egress_line_id = line.id
    db.session.commit()
    return {"ok": True, "egress_ip": device.egress_ip, "line": line.name}


def unbind(device: Device) -> None:
    from .routes.vpc import _clear_line

    _clear_line(device)
    device.egress_line_id = None
    db.session.commit()


def maybe_bind(device: Device) -> dict:
    """分配设备时自动绑一条出口（没有可用线路就跳过，不影响分配）。"""
    if device is None or device.egress_ip:
        return {"ok": True, "skipped": "已有出口"}
    line = pick_line()
    if line is None:
        return {"ok": True, "skipped": "无可用线路"}
    try:
        return bind(device, line)
    except Exception as e:  # noqa: BLE001 - 绑出口失败不能影响设备分配
        return {"ok": False, "error": str(e)}


def bind_batch(limit: int = 50, only_unbound: bool = True) -> dict:
    """给一批设备轮转绑定出口线路（一条线最多摊到尽量均）。

    返回 {ok, bound, skipped, failed:[{device_id, error}]}
    """
    lines = running_lines()
    if not lines:
        return {"ok": False, "error": "没有可用（running）的出口线路",
                "bound": 0, "skipped": 0, "failed": []}
    q = db.session.query(Device).filter(Device.backend == "redroid")
    if only_unbound:
        q = q.filter((Device.egress_ip.is_(None)) | (Device.egress_ip == ""))
    devices = q.order_by(Device.id).limit(limit).all()

    bound, failed = 0, []
    load = {ln.id: _line_load(ln.id) for ln in lines}
    for d in devices:
        line = min(lines, key=lambda ln: load.get(ln.id, 0))
        try:
            res = bind(d, line)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": str(e)}
        if res.get("ok"):
            bound += 1
            load[line.id] = load.get(line.id, 0) + 1
        else:
            failed.append({"device_id": d.id, "name": d.name, "error": res.get("error")})
    return {"ok": True, "bound": bound, "skipped": len(devices) - bound,
            "failed": failed[:20], "lines": len(lines)}
