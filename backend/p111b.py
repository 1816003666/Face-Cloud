# -*- coding: utf-8 -*-
import paramiko, time
def run(cmd, t=30, sudo=False):
    if sudo:
        cmd = "echo Passw0rd | sudo -S -p '' bash -c '%s' 2>&1" % cmd.replace("'", "'\''")
    try:
        c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect("192.168.99.111", port=22, username="firefly", password="Passw0rd", timeout=20,
                  look_for_keys=False, allow_agent=False, banner_timeout=30, auth_timeout=30)
        _, out, err = c.exec_command(cmd, timeout=t)
        o = out.read().decode(errors="replace"); e = err.read().decode(errors="replace")
        c.close(); return o, e
    except Exception as ex:
        return "", "CONN EXC: %s: %s" % (type(ex).__name__, ex)
print("== 111 hostname / uname ==")
o,e = run("hostname; hostname -I 2>/dev/null; cat /etc/hostname 2>/dev/null"); print(o or "(empty)", e)
print("== 111 资源 ==")
o,e = run("uptime; echo ---; free -m | head -3; echo ---; nproc"); print(o or "(empty)", e)
print("== 111 docker ps (sudo) ==")
o,e = run('docker ps -a --filter "name=redroid" --format "{{.Names}}|{{.Status}}|{{.Ports}}"', 40, sudo=True)
print(o or "(empty)")
if e: print("[err]", e[:300])
print("== 111 容器 IP (docker inspect 简版) ==")
o,e = run('docker ps --format "{{.Names}}" | head -20 | while read n; do ip=$(docker inspect -f "{{.Name}} {{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}" "$n" 2>/dev/null); echo "$ip"; done', 60, sudo=True)
print(o or "(empty)")
