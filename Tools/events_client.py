#!/usr/bin/env python3
"""Simulasi klien iOS via CLI — menerima alert server secara realtime (SSE).

Inilah yang nanti diterima app HSE setelah integrasi UI: login dengan akun
role hse, lalu stream /events. Hanya stdlib — jalan dari .venv server.

Usage:
  .venv/bin/python Tools/events_client.py --username hse-demo --password '<password>'
  (Ctrl+C berhenti)
"""
import argparse
import http.client
import json
import sys
import time


def parse_block(block):
    fields = {}
    for line in block.splitlines():
        if line.startswith(':'):  # komentar/heartbeat SSE
            continue
        field, sep, value = line.partition(':')
        if sep:
            fields[field.strip()] = value.lstrip()
    return fields or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--username', required=True)
    ap.add_argument('--password', required=True)
    ap.add_argument('--max-seconds', type=float, default=None, help='berhenti setelah N detik (demo)')
    args = ap.parse_args()

    connection = http.client.HTTPConnection(args.host, args.port, timeout=30)
    body = json.dumps({'username': args.username, 'password': args.password})
    connection.request('POST', '/auth/login', body, {'Content-Type': 'application/json'})
    response = connection.getresponse()
    raw = response.read()
    if response.status != 200:
        print(f'login gagal ({response.status}): {raw.decode()}', file=sys.stderr)
        return 1
    token = json.loads(raw)['access_token']
    print(f'login ok — streaming /events (Ctrl+C untuk berhenti)', flush=True)

    connection.request('GET', '/events', headers={'Authorization': f'Bearer {token}'})
    response = connection.getresponse()
    if response.status != 200:
        print(f'/events ditolak ({response.status}) — role harus hse.', file=sys.stderr)
        return 1

    received = 0
    buffer = ''
    deadline = time.monotonic() + args.max_seconds if args.max_seconds else None
    try:
        while True:
            try:
                chunk = response.read1(4096)  # satu read socket: data yg tersedia saja
            except TimeoutError:
                # Server mengirim heartbeat tiap 10 detik; jeda lebih panjang jarang.
                if deadline and time.monotonic() > deadline:
                    break
                continue
            if not chunk:
                break  # koneksi ditutup server
            buffer += chunk.decode('utf-8', 'replace')
            while '\n\n' in buffer:
                raw_block, buffer = buffer.split('\n\n', 1)
                event = parse_block(raw_block)
                if not event or 'data' not in event:
                    continue  # heartbeat / komentar
                try:
                    payload = json.loads(event['data'])
                except json.JSONDecodeError:
                    continue
                received += 1
                clock = time.strftime('%H:%M:%S')
                print(f'[{clock}] 🔔 seq {payload.get("sequence", "?")} — {payload.get("message", "?")}', flush=True)
                alert = payload.get('alert', {})
                extras = {k: v for k, v in alert.items() if k != 'message'}
                if extras:
                    print(f'           {extras}', flush=True)
            if deadline and time.monotonic() > deadline:
                break
    except KeyboardInterrupt:
        pass
    print(f'selesai — {received} event diterima.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
