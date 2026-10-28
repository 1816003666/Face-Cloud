import socket, base64, os, time
udid = '192.168.99.109:5555'
path = '/?action=proxy-adb&remote=tcp:8886&udid=' + udid.replace(':', '%3A')
key = base64.b64encode(os.urandom(16)).decode()
req = (
    'GET %s HTTP/1.1\r\n'
    'Host: 127.0.0.1:8100\r\n'
    'Upgrade: websocket\r\n'
    'Connection: Upgrade\r\n'
    'Sec-WebSocket-Key: %s\r\n'
    'Sec-WebSocket-Version: 13\r\n'
    'Origin: http://112.4.135.254:8100\r\n\r\n'
) % (path, key)
s = socket.create_connection(('127.0.0.1', 8100), timeout=8)
s.sendall(req.encode())
s.settimeout(3)
try:
    resp = s.recv(4096)
    print('RESP:', resp[:200])
    ok = resp.startswith(b'HTTP/1.1 101')
    print('WS_UPGRADE:', 'OK' if ok else 'FAIL')
    if ok:
        # keep open 3s, count bytes
        s.settimeout(2)
        total = 0
        t0 = time.time()
        while time.time() - t0 < 3:
            try:
                d = s.recv(4096)
                if not d: break
                total += len(d)
            except socket.timeout:
                continue
        print('RECV_BYTES_3s:', total)
        print('CONNECTION: KEPT' if total >= 0 else 'CLOSED')
except Exception as ex:
    print('ERR:', type(ex).__name__, str(ex)[:200])
finally:
    try: s.close()
    except: pass
