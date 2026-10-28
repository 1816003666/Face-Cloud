# -*- coding: utf-8 -*-
"""启动时补齐数据库缺失的列（轻量 schema 自检）。

背景：这个项目的数据库是从备份恢复过来的，而模型（models.py）一直在加字段。
`db.create_all()` 只会**新建缺失的表**，不会给已有表加列——于是代码里加了
`Device.egress_ip` 之后，任何对 devices 的查询都会直接
`sqlite3.OperationalError: no such column: devices.egress_ip` → 接口 500。

这里在启动时对比模型与实际表结构，把缺的列 ALTER 补上（只加列、不删不改，
失败也只在日志里提示，不阻塞启动）。
"""
from __future__ import annotations


def ensure_schema(app):
    """给已有表补齐模型里新加的列，返回补上的列清单。"""
    from .extensions import db

    fixed = []
    try:
        with app.app_context():
            engine = db.engine
            if engine.dialect.name != "sqlite":
                return fixed          # 生产库走正式迁移，这里只兜 SQLite
            with engine.begin() as conn:
                from sqlalchemy import Boolean, inspect, text
                insp = inspect(engine)
                existing_tables = set(insp.get_table_names())
                for table in db.metadata.sorted_tables:
                    if table.name not in existing_tables:
                        continue      # 新表交给 create_all
                    real_cols = {c["name"] for c in insp.get_columns(table.name)}
                    for col in table.columns:
                        if col.name in real_cols:
                            continue
                        ddl = (f'ALTER TABLE {table.name} ADD COLUMN {col.name} '
                               f'{col.type.compile(engine.dialect)}')
                        default = getattr(col.default, "arg", None)
                        if default is not None and not callable(default):
                            # 布尔列要写成 0/1：早期版本写成 'False' 字符串，
                            # 导致布尔判断和 IS NOT 1 的查询全部失效。
                            if isinstance(default, bool):
                                ddl += f" DEFAULT {1 if default else 0}"
                            elif isinstance(default, (int, float)):
                                ddl += f" DEFAULT {default}"
                            else:
                                ddl += f" DEFAULT '{default}'"
                        conn.execute(text(ddl))
                        fixed.append(f"{table.name}.{col.name}")

                # 修复历史遗留：布尔列里被写成文本 'True'/'False' 的值统一改成 1/0
                for table in db.metadata.sorted_tables:
                    if table.name not in existing_tables:
                        continue
                    for col in table.columns:
                        if not isinstance(col.type, Boolean):
                            continue
                        conn.execute(text(
                            f"UPDATE {table.name} SET {col.name} = "
                            f"CASE WHEN lower(CAST({col.name} AS TEXT)) IN ('true','1') "
                            f"THEN 1 ELSE 0 END "
                            f"WHERE typeof({col.name}) = 'text'"))
    except Exception as e:  # noqa: BLE001 - 自检失败不应影响服务启动
        print(f"[schema] 补列失败（可忽略，但相关接口可能报 500）: {e}", flush=True)
        return fixed
    if fixed:
        print(f"[schema] 已补齐缺失的列: {', '.join(fixed)}", flush=True)
    return fixed
