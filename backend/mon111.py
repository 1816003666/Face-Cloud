# -*- coding: utf-8 -*-
import subprocess, sqlite3, time
d = sqlite3.connect("instance/cloud_phone.db")
serials = [r[0] for r in d.execute("select serial from devices where ip='192.168.99.111' and status='running'")]
# 确保 adb connect 全部
for s in serials:
    subprocess.run(["adb","connect",s],capture_output=True,timeout=8)
R = 24   # 轮数
INT = 5  # 每轮间隔秒
fails = {s: 0 for s in serials}
states = {s: "" for s in serials}
for r in range(R):
    out = subprocess.run(["adb","devices"],capture_output=True,timeout=8).stdout.decode(errors="replace")
    lines = {l.split()[0]: l.split()[1] for l in out.splitlines()[1:] if len(l.split())>=2}
    now = []
    for s in serials:
        st = lines.get(s, "MISSING")
        if st != "device":
            fails[s] += 1
            # 尝试重连
            subprocess.run(["adb","connect",s],capture_output=True,timeout=8)
            out2 = subprocess.run(["adb","devices"],capture_output=True,timeout=8).stdout.decode(errors="replace")
            l2 = {l.split()[0]: l.split()[1] for l in out2.splitlines()[1:] if len(l.split())>=2}
            st2 = l2.get(s, "MISSING")
            now.append(f"{s}->{st}->{st2}")
        else:
            now.append(f"{s}->ok")
    print(f"round {r+1}: " + "; ".join(now), flush=True)
    time.sleep(INT)
print("== 掉线统计 ==")
for s,c in sorted(fails.items(), key=lambda x:-x[1]):
    if c: print(f"  {s}: 掉线 {c}/{R} 轮")
print("总掉线设备:", sum(1 for c in fails.values() if c), "/", len(serials))
