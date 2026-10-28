import sqlite3
db = sqlite3.connect('/home/firefly/cloud-phone/backend/instance/cloud_phone.db')
tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print('tables:', tables)
for t in tables:
    try:
        n = db.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0]
        print(t, 'rows:', n)
    except Exception as ex:
        print(t, 'ERR', ex)
print('sample devices:', db.execute('SELECT id,name,serial,status FROM devices LIMIT 5').fetchall())
