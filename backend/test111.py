# -*- coding: utf-8 -*-
import sqlite3, subprocess, time, concurrent.futures
d = sqlite3.connect("instance/cloud_phone.db")
serials = [r[0] for r in d.execute("select serial from devices where ip='192.168.99.111' and status='running'")]
print("111 running serials:", len(serials))
def test(s):
    t = time.time()
    subprocess.run(["adb","connect",s],capture_output=True,timeout=10)
    r = subprocess.run(["adb","-s",s,"get-state"],capture_output=True,timeout=8)
    ms = int((time.time()-t)*1000)
    return s, r.returncode==0, r.stdout.decode(errors="replace").strip(), ms
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
    results = list(ex.map(test, serials))
ok = sum(1 for _,o,_,_ in results if o)
print(f"在线 {ok}/{len(results)}")
for s,o,st,ms in sorted(results, key=lambda x: x[1], reverse=True):
    print(f"  {s} -> {'OK' if o else 'FAIL'} state={st} {ms}ms")
