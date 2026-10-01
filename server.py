#!/usr/bin/env python3
"""Minimal HTTP/1.1 chunk server with application-informed pacing.

The client asks for a pace rate per request with the X-Pace-Rate header (bit/s),
the same ABR-to-server signal Sammy uses. The server applies it with
SO_MAX_PACING_RATE. Since Linux 4.13 the kernel enforces that through TCP
internal pacing (hrtimer based), so no fq qdisc and no NET_ADMIN are needed.

X-Pace-Rate: 0 or a missing header leaves the socket untouched (unpaced).
Setting any finite rate switches the socket to SK_PACING_NEEDED for the rest of
its life, so the client must open a fresh connection per pacing condition.

Bodies are sent with sendfile() from a page-cached random file, so Python adds
almost no per-byte work and the kernel does the transfer.
"""
import os
import socket
import struct
import sys
import threading
import time

SO_MAX_PACING_RATE = getattr(socket, 'SO_MAX_PACING_RATE', 47)
PORT = int(os.environ.get('PORT', '8080'))
PAYLOAD = '/tmp/payload.bin'
PAYLOAD_BYTES = int(os.environ.get('PAYLOAD_MB', '64')) * 1024 * 1024


def make_payload():
    with open(PAYLOAD, 'wb') as f:
        left = PAYLOAD_BYTES
        while left:
            n = min(left, 1 << 20)
            f.write(os.urandom(n))
            left -= n


def read_request(conn, buf):
    while b'\r\n\r\n' not in buf:
        data = conn.recv(4096)
        if not data:
            return None, None, buf
        buf += data
    head, buf = buf.split(b'\r\n\r\n', 1)
    lines = head.decode('latin-1').split('\r\n')
    target = lines[0].split(' ')[1]
    headers = {}
    for line in lines[1:]:
        if ':' in line:
            key, value = line.split(':', 1)
            headers[key.strip().lower()] = value.strip()
    return target, headers, buf


def handle(conn, peer):
    paced_at = 0
    requests = sent = 0
    cpu_start = time.thread_time()
    payload = open(PAYLOAD, 'rb')
    buf = b''
    try:
        while True:
            target, headers, buf = read_request(conn, buf)
            if target is None:
                break
            path, _, query = target.partition('?')
            params = dict(p.split('=', 1) for p in query.split('&') if '=' in p)
            size = int(params.get('bytes', '0')) if path == '/chunk' else 0

            pace = int(headers.get('x-pace-rate', '0'))
            if pace > 0 and pace != paced_at:
                conn.setsockopt(socket.SOL_SOCKET, SO_MAX_PACING_RATE, struct.pack('Q', pace // 8))
                paced_at = pace

            conn.sendall((
                'HTTP/1.1 200 OK\r\n'
                'Content-Type: application/octet-stream\r\n'
                f'Content-Length: {size}\r\n'
                'Connection: keep-alive\r\n\r\n'
            ).encode())
            left = size
            while left:
                n = min(left, PAYLOAD_BYTES)
                conn.sendfile(payload, 0, n)
                left -= n
            requests += 1
            sent += size
    except (ConnectionError, OSError) as exc:
        print(f'{peer}: {exc}', file=sys.stderr, flush=True)
    finally:
        payload.close()
        conn.close()
        if requests:
            print(f'conn {peer[0]}:{peer[1]} pace_bps={paced_at} requests={requests} '
                  f'bytes={sent} thread_cpu_s={time.thread_time() - cpu_start:.3f}', flush=True)


def main():
    make_payload()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', PORT))
    srv.listen(64)
    print(f'listening on :{PORT}, payload {PAYLOAD_BYTES} bytes', flush=True)
    while True:
        conn, peer = srv.accept()
        threading.Thread(target=handle, args=(conn, peer), daemon=True).start()


if __name__ == '__main__':
    main()
