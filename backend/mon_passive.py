# -*- coding: utf-8 -*-
import subprocess, time
serials = [f"192.168.99.{a}:{p}" for a in range(101,111) for p in (5555,5556)]
serials.sort()
R = 18; INT = 10
seen = {}
for r in range(R):
    out = subprocess.run(["adb","devices"],capture_output=True,timeout=10).stdout.decode(errors="replace")
    lines = {l.split()[0]: l.split()[1] for l in out.splitlines()[1:] if len(l.split())>=2}
    missing = [s for s in serials if lines.get(s)!="device"]
    bad = [f"{s}={lines.get(s)}" for s in serials if s in lines and lines[s]!="device"]
    for s in serials:
        seen.setdefault(s,0)
        if lines.get(s)!="device": seen[s]+=1
    t = time.strftime("%H:%M:%S")
    print(f"{t} r{r+1}: 在线 {len(serials)-len(missing)}/{len(serials)}", flush=True)
    if missing: print(f"   缺失/异常: {missing if not bad else bad}", flush=True)
    time.sleep(INT)
print("== 累计异常轮次 ==")
for s,c in sorted(seen.items(), key=lambda x:-x[1]):
    if c: print(f"  {s}: {c}/{R}")
print("无异常设备:", sum(1 for c in seen.values() if c==0), "/", len(serials))
