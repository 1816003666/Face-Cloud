import sqlite3
db = sqlite3.connect('/home/firefly/cloud-phone/backend/instance/cloud_phone.db')
rows = db.execute("SELECT id, name, serial FROM devices WHERE serial LIKE '192.168.99.%:5555' ORDER BY id").fetchall()
for did, name, serial in rows:
    octet = serial.split('.')[-1].split(':')[0]
    db.execute("UPDATE devices SET name=? WHERE id=?", (octet, did))
db.commit()
print('renamed', len(rows), 'devices')
for did, name, serial in db.execute("SELECT id, name, serial FROM devices ORDER BY id"):
    print(did, name, serial)
