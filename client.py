#!/usr/bin/env python3
"""Steady-state video player model for the pacing energy experiment.

One call is one GMT phase. It opens a fresh keep-alive connection (see
server.py for why it must be fresh), requests one chunk of
bitrate * chunk_seconds bytes every chunk_seconds, asks the server for a pace
rate via X-Pace-Rate and discards the body. Every phase moves the same bytes
over the same wall time; only the on-period throughput changes. That isolates
the question: does spreading the same work out in time cost energy?

At the end it prints one JSON summary line plus GMT custom metric lines, and
host-wide counters for the phase (context switches, softirqs, C-state
residency) so an energy difference can be tied to a mechanism.
"""
import argparse
import glob
import json
import os
import re
import resource
import socket
import time


def read_counters():
    c = {}
    with open('/proc/stat') as f:
        for line in f:
            key, *vals = line.split()
            if key in ('ctxt', 'intr', 'softirq'):
                c[key] = int(vals[0])
    with open('/proc/softirqs') as f:
        next(f)
        for line in f:
            name, *vals = line.split()
            c['softirq_' + name.rstrip(':').lower()] = sum(int(v) for v in vals)
    for state in glob.glob('/sys/devices/system/cpu/cpu[0-9]*/cpuidle/state[0-9]*'):
        try:
            with open(f'{state}/name') as f:
                name = f.read().strip()
            with open(f'{state}/time') as f:
                t = int(f.read())
            with open(f'{state}/usage') as f:
                u = int(f.read())
        except OSError:
            continue
        c[f'cstate_{name}_time_us'] = c.get(f'cstate_{name}_time_us', 0) + t
        c[f'cstate_{name}_entries'] = c.get(f'cstate_{name}_entries', 0) + u
    return c


def connect(host, port, deadline):
    while True:
        try:
            return socket.create_connection((host, port), timeout=30)
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def fetch(sock, host, size, pace_bps, buf):
    sock.sendall((
        f'GET /chunk?bytes={size} HTTP/1.1\r\n'
        f'Host: {host}\r\n'
        f'X-Pace-Rate: {pace_bps}\r\n\r\n'
    ).encode())
    head = b''
    while b'\r\n\r\n' not in head:
        data = sock.recv(4096)
        if not data:
            raise ConnectionError('server closed connection')
        head += data
    head, body = head.split(b'\r\n\r\n', 1)
    length = next(int(line.split(b':', 1)[1]) for line in head.split(b'\r\n')
                  if line.lower().startswith(b'content-length'))
    got, calls = len(body), 0
    view = memoryview(buf)
    while got < length:
        n = sock.recv_into(view[:min(len(buf), length - got)])
        if not n:
            raise ConnectionError('short body')
        got += n
        calls += 1
    return calls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--server', default='server:8080')
    ap.add_argument('--label', required=True)
    ap.add_argument('--pace-mbps', type=float, default=0, help='0 = unpaced')
    ap.add_argument('--bitrate-mbps', type=float, default=16)
    ap.add_argument('--chunk-seconds', type=float, default=4)
    ap.add_argument('--duration', type=float, default=60)
    ap.add_argument('--idle', action='store_true', help='no traffic, only record counters')
    args = ap.parse_args()

    host, port = args.server.rsplit(':', 1)
    chunk_bytes = int(args.bitrate_mbps * 1e6 / 8 * args.chunk_seconds)
    pace_bps = int(args.pace_mbps * 1e6)
    n_chunks = 0 if args.idle else int(args.duration // args.chunk_seconds)
    buf = bytearray(1 << 20)

    sock = None if args.idle else connect(host, int(port), time.monotonic() + 120)
    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    c0 = read_counters()
    t0 = time.monotonic()

    dl_times, recv_calls, late = [], 0, 0
    for i in range(n_chunks):
        due = t0 + i * args.chunk_seconds
        now = time.monotonic()
        if now < due:
            time.sleep(due - now)
        elif i:
            late += 1
        start = time.monotonic()
        recv_calls += fetch(sock, host, chunk_bytes, pace_bps, buf)
        dl_times.append(time.monotonic() - start)

    end = t0 + args.duration
    if time.monotonic() < end:
        time.sleep(end - time.monotonic())
    wall = time.monotonic() - t0
    c1 = read_counters()
    ru1 = resource.getrusage(resource.RUSAGE_SELF)
    if sock:
        sock.close()

    delta = {k: c1[k] - c0.get(k, 0) for k in c1}
    ncpu = os.cpu_count()
    cstate_share = {k[len('cstate_'):-len('_time_us')]: round(v / (wall * 1e6 * ncpu), 4)
                    for k, v in delta.items() if k.endswith('_time_us')}
    deep = sum(v for k, v in cstate_share.items()
               if (m := re.match(r'C(\d+)', k)) and int(m.group(1)) >= 6)
    on_time = sum(dl_times)
    total_bytes = chunk_bytes * len(dl_times)
    onperiod_bps = total_bytes * 8 / on_time if on_time else 0
    summary = {
        'label': args.label,
        'pace_mbps': args.pace_mbps,
        'bitrate_mbps': args.bitrate_mbps,
        'chunks': len(dl_times),
        'bytes': total_bytes,
        'wall_s': round(wall, 3),
        'on_time_s': round(on_time, 3),
        'duty_cycle': round(on_time / wall, 4),
        'onperiod_mbps': round(onperiod_bps / 1e6, 1),
        'chunk_dl_s_max': round(max(dl_times, default=0), 3),
        'late_chunks': late,
        'client_recv_calls': recv_calls,
        'client_cpu_s': round(ru1.ru_utime + ru1.ru_stime - ru0.ru_utime - ru0.ru_stime, 3),
        'client_vol_ctx_switches': ru1.ru_nvcsw - ru0.ru_nvcsw,
        'host_ctxt_per_s': round(delta.get('ctxt', 0) / wall),
        'host_intr_per_s': round(delta.get('intr', 0) / wall),
        'host_softirq_per_s': {k[len('softirq_'):]: round(v / wall) for k, v in delta.items()
                               if k.startswith('softirq_') and v},
        'cstate_residency': cstate_share,
        'cstate_deep_residency': round(deep, 4),
    }
    print('PACER_SUMMARY ' + json.dumps(summary), flush=True)

    ts = time.time_ns() // 1000
    for key, value in (
        ('onperiod_kbps', onperiod_bps / 1e3),
        ('duty_permille', on_time / wall * 1e3),
        ('host_ctxt_per_s', delta.get('ctxt', 0) / wall),
        ('deep_cstate_permille', deep * 1e3),
        ('late_chunks', late),
    ):
        print(f'{ts} {key}={int(value)}', flush=True)


if __name__ == '__main__':
    main()
