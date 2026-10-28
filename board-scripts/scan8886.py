import subprocess
ips = ['192.168.99.%d' % n for n in list(range(101,111)) + list(range(112,122))]
for ip in ips:
    ser = ip + ':5555'
    r = subprocess.run(['adb','connect',ser], capture_output=True, text=True, timeout=20)
    r2 = subprocess.run(['adb','-s',ser,'shell','cat /proc/net/tcp6'], capture_output=True, text=True, timeout=20)
    ok = '22B6' in r2.stdout
    print(ip, '8886=OK' if ok else '8886=NO')
