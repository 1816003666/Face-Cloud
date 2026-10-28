# -*- coding: utf-8 -*-
"""统一门户内网接口（门户 → 产品）。

- GET  /api/internal/products   商品目录（门户每 10 分钟拉一次）
- POST /api/internal/provision  开通（P3 实现；当前明确返回 5001，绝不假成功）

两个接口都只认 X-Internal-Key，响应统一套 {code, message, data}。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, current_app, request

from ..portal import require_internal_key, build_products

internal_bp = Blueprint("internal", __name__)


@internal_bp.get("/internal/products")
@require_internal_key
def internal_products():
    if not current_app.config.get("PORTAL_ENABLED", True):
        return jsonify(code=5001, message="portal disabled", data=None), 200
    products = build_products()
    if not products:
        # 门户契约警告（门户侧对接说明 §2.1）：code=0 + 空数组 = 门户把云手机
        # 所有在售商品软下架。没有可售商品时按"同步失败"返回，门户会保留上一次
        # 成功的目录，不会误下架。
        return jsonify(code=5001, message="没有可售商品（按同步失败处理，避免门户下架）",
                       data=None), 200
    return jsonify(code=0, message="ok", data={"products": products})


@internal_bp.post("/internal/provision")
@require_internal_key
def internal_provision():
    """开通（幂等）：实现见 app/portal_provision.py。30 秒内返回，建机在后台补。"""
    from ..portal_provision import handle_provision
    body, status = handle_provision(request.get_json(silent=True) or {})
    return jsonify(body), status
