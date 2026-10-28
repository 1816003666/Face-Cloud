import sqlite3, datetime, os

db_path = os.path.join(os.path.dirname(__file__), "instance", "cloud_phone.db")
conn = sqlite3.connect(db_path)
cur = conn.cursor()

now = datetime.datetime.utcnow().isoformat()
cur.execute("UPDATE devices SET status='running', last_seen=? WHERE backend='redroid'", (now,))
conn.commit()

cur.execute("SELECT id, name, status, last_seen FROM devices")
for row in cur.fetchall():
    print(row)

conn.close()
print("Done - devices updated to running")
