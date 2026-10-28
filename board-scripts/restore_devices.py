import sqlite3
cur_db = '/home/firefly/cloud-phone/backend/instance/cloud_phone.db'
bak_db = '/home/firefly/cloud-phone/backend/instance/cloud_phone.db.bak_rename'
b = sqlite3.connect(bak_db)
rows = b.execute('SELECT * FROM devices').fetchall()
cols = [d[0] for d in b.execute('SELECT * FROM devices LIMIT 0').description]
print('bak devices:', len(rows), 'cols:', cols)
cur = sqlite3.connect(cur_db)
cur.execute('DELETE FROM devices')
ph = ','.join('?' * len(cols))
for r in rows:
    cur.execute('INSERT INTO devices (' + ','.join(cols) + ') VALUES (' + ph + ')', r)
cur.commit()
# rename to IP octet
db = cur
upd = db.execute("SELECT id, serial FROM devices WHERE serial LIKE '192.168.99.%:5555'").fetchall()
for did, serial in upd:
    octet = serial.split('.')[-1].split(':')[0]
    db.execute('UPDATE devices SET name=? WHERE id=?', (octet, did))
db.commit()
print('restored + renamed:', len(upd))
for did, name, serial, status in db.execute('SELECT id,name,serial,status FROM devices ORDER BY id'):
    print(did, name, serial, status)
