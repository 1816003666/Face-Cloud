# -*- coding: utf-8 -*-
import paramiko, sys
def run(cmd, t=30):
    try:
        c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect("192.168.99.111", port=22, username="firefly", password="Passw0rd", timeout=20,
                  look_for_keys=False, allow_agent=False, banner_timeout=30, auth_timeout=30)
        _, out, err = c.exec_command(cmd, timeout=t)
        o = out.read().decode(errors="replace")
        e = err.read().decode(errors="replace")
        c.close(); return o, e
    except Exception as ex:
        return "", "CONN EXC: %s: %s" % (type(ex).__name__, ex)
o, e = run('docker ps --format "{{.ID}} {{.Names}} {{.Ports}}" 2>&1 | head -40')
print("== 111 docker ps ==")
print(o or "(empty)")
print("== network ==")
o2, e2 = run("docker network ls 2>&1 | head; echo ---; ip -4 addr show 2>/dev/null | grep 'inet ' | head")
print(o2 or "(empty)")
if e or e2: print("[err]", e, e2)
