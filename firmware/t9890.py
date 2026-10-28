import socket, sys
s = socket.create_connection(('192.168.207.224', 9890), timeout=15)
s.sendall(b'GET /fn/listen.wav HTTP/1.0\r\nHost: x\r\n\r\n')
data = b''
while True:
    d = s.recv(65536)
    if not d: break
    data += d
    if len(data) > 10 and b'\r\n\r\n' in data:
        hdr, body = data.split(b'\r\n\r\n', 1)
        cl = [l for l in hdr.split(b'\r\n') if l.lower().startswith(b'content-length')]
        print(hdr.decode('latin1'), '| got', len(body), flush=True)
        if cl and len(body) >= int(cl[0].split(b':')[1]):
            break
print('total', len(data))
open('listen.wav','wb').write(data.split(b'\r\n\r\n',1)[1])
