import paramiko
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    c.connect('192.168.99.122', port=22, username='firefly', password='Passw0rd', timeout=10)
    print('LOGIN_OK')
    for cmd in ['hostname', 'whoami', "ip -4 addr show | grep -E 'inet '", 'which adb; adb devices 2>&1 | head -30']:
        i, o, e = c.exec_command(cmd, timeout=25)
        print('## ' + cmd)
        print(o.read().decode('utf-8','replace'))
        err = e.read().decode('utf-8','replace')
        if err.strip(): print('[err]', err[:500])
    c.close()
except Exception as ex:
    print('LOGIN_FAIL:', type(ex).__name__, str(ex)[:300])
