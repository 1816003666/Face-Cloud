import paramiko
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('192.168.99.122', port=22, username='firefly', password='Passw0rd', timeout=10)
cmds = [
    ('adb devices -l', 25),
    ('lsusb | wc -l; lsusb | head -30', 25),
    ('docker ps -a 2>&1 | head -20', 25),
    ('ls ~', 15),
    ('ls ~/redroid* 2>/dev/null; ls /data 2>/dev/null | head', 15),
    ('cat /etc/os-release | head -3', 15),
    ('cat /etc/hosts', 15),
    ('grep -rn "5555\|adb connect" ~/*.sh ~/*.py 2>/dev/null | head -20', 15),
]
for cmd, t in cmds:
    i, o, e = c.exec_command(cmd, timeout=t)
    out = o.read().decode('utf-8','replace')
    err = e.read().decode('utf-8','replace')
    print('## ' + cmd)
    print(out)
    if err.strip(): print('[err]', err[:300])
c.close()
