# -*- coding: utf-8 -*-
import sqlite3
d = sqlite3.connect("instance/cloud_phone.db")
for ip in ["192.168.99.111", "192.168.99.112"]:
    rows = d.execute("select id,name,serial,ip,status from devices where ip=? order by id", (ip,)).fetchall()
    print(f"== ip={ip} 设备 {len(rows)} 台 ==")
    for r in rows:
        print("  ", r)
