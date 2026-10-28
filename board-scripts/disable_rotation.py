import sqlite3
db = sqlite3.connect('/home/firefly/cloud-phone/backend/instance/cloud_phone.db')
print('rotation before:', db.execute('SELECT * FROM rotation_config').fetchall())
db.execute('UPDATE rotation_config SET enabled=0, started_at=NULL, next_round_at=NULL, round_index=0')
db.commit()
print('rotation after:', db.execute('SELECT * FROM rotation_config').fetchall())
print('devices:', db.execute('SELECT COUNT(*) FROM devices').fetchone()[0])
