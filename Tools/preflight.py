#!/usr/bin/env python3
"""Pre-flight readiness HARNES — gerbang "SIAP UJI" untuk satu pintu demo.

Keputusan owner 14 Nov 2026 (arsitektur demo-ready): kejadian "semua nyala
tapi unknown/connecting" tidak boleh dideteksi dengan menebak — readiness
harus DIUKUR. Tool ini memeriksa jalur uji end-to-end TANPA mengubah apa pun:

1. /health           — server hidup + model deteksi termuat
2. login HSE         — otentikasi demo jalan (kredensial relogin_demo)
3. kamera desired    — snapshot 200 + ETag BERGERAK dua kali poll = frame
                       sungguhan mengalir dan di-encode (preview siap)
4. unit operating    — /equipment/status: operationActive True tiap unit
                       (monitoringStatus dicetak sebagai info; "unknown"
                       sah saat excavator memang di luar bidang)

Pemakaian: harnes `demo` memanggil tool ini; berdiri sendiri juga bisa:
    .venv/bin/python Tools/preflight.py --port 8080 [--timeout 90]
Exit 0 = SIAP UJI; nonzero = cetak baris gagal. Loop retry sampai deadline
supaya boot server (muat model MPS ±15 dtk) tidak bikin lonceng palsu.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Sources.infrastructure.database.connection import connect  # unit demo


def operator_unit(operator, connection_factory=connect):
    """equipment_instance_id assignment aktif milik operator demo (atau None).
    Query DB langsung — pola Tools/relogin_demo.py: readiness mengadili UNIT
    operator demo, sisanya hanya baris info."""
    with connection_factory() as connection:
        row = connection.execute(
            "SELECT a.equipment_instance_id FROM assignments a "
            "JOIN users u ON u.id = a.operator_id "
            "WHERE a.status='active' AND u.is_active AND u.role='operator' "
            "AND u.username=%s "
            "ORDER BY a.assigned_at_ms DESC LIMIT 1", (operator,)).fetchone()
    return row[0] if row else None


def make_transport(base_url, timeout=8):
    """Transport urllib: (method, path, body=None, token=None) -> (status, json, headers).

    Di-inject supaya test dapat memalsukan seluruh jalur HTTP tanpa server.
    Error jaringan → status 0 (server belum dengar)."""

    def transport(method, path, body=None, token=None):
        request = urllib.request.Request(
            base_url + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json',
                     **({'Authorization': 'Bearer ' + token} if token else {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                content_type = response.headers.get('Content-Type', '')
                # Body biner (JPEG snapshot) tidak diparse — cukup status+headers.
                parsed = json.loads(raw) if (raw and 'json' in content_type) else None
                return response.status, parsed, dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, None, dict(error.headers or {})
        except (urllib.error.URLError, TimeoutError, OSError):
            return 0, None, {}

    return transport


def check_once(transport, username, password, poll_gap_s=2.0,
               camera_target=None, operator_equipment=None):
    """Satu putaran pemeriksaan → list baris (label, ok, note) + hse token.

    Kamera cek "frame bergerak": dua snapshot dengan jeda — ETag berubah =
    pipeline playback→encode mongering. ETag sama/503 = gejala lama
    (unknown/connecting) terdeteksi lebih awal."""
    lines = []
    token = None

    status, body, _ = transport('GET', '/health')
    ok = status == 200 and bool(body and body.get('ok'))
    model = bool(body and body.get('modelLoaded'))
    lines.append(('server /health', ok,
                  'modelLoaded=' + str(model) if ok else f'HTTP {status}'))
    if not ok:
        return lines, token

    status, body, _ = transport('POST', '/auth/login',
                                {'username': username, 'password': password})
    ok = status == 200 and bool(body and body.get('access_token'))
    if ok:
        token = body['access_token']
        lines.append((f'login HSE ({username})', True, 'ok'))
    else:
        lines.append((f'login HSE ({username})', False, f'HTTP {status}'))
        return lines, token

    status, body, _ = transport('GET', '/cameras', token=token)
    cameras = body.get('items', []) if status == 200 and body else None
    if cameras is None:
        lines.append(('daftar kamera', False, f'HTTP {status}'))
    else:
        lines.append(('daftar kamera', True, f'{len(cameras)} terdaftar'))
        for camera in cameras:
            label = f"kamera '{camera['name']}'"
            if camera_target and camera['name'] != camera_target:
                lines.append((label, True, f'info — bukan kamera demo ({camera_target})'))
                continue
            if not camera.get('desiredOn'):
                lines.append((label, True, 'idle — desired off (disengaja)'))
                continue
            path = f"/cameras/{camera['id']}/snapshot.jpg"
            status1, _, headers1 = transport('GET', path, token=token)
            if status1 != 200:
                lines.append((label, False,
                              f'snapshot HTTP {status1} — frame belum ada'))
                continue
            time.sleep(poll_gap_s)
            status2, _, headers2 = transport('GET', path, token=token)
            moved = status2 == 200 and headers2.get('ETag') != headers1.get('ETag')
            if status2 != 200:
                lines.append((label, False, f'snapshot kedua HTTP {status2}'))
            elif moved:
                lines.append((label, True, 'frame mengalir (ETag bergerak)'))
            else:
                lines.append((label, False, 'frame beku — ETag tak berubah'))

    status, body, _ = transport('GET', '/equipment/status', token=token)
    items = body.get('items', []) if status == 200 and body else None
    if items is None:
        lines.append(('status unit', False, f'HTTP {status}'))
    elif not items:
        lines.append(('status unit', False, 'belum ada unit ter-daftar'))
    else:
        for item in items:
            active = bool(item.get('operationActive'))
            note = (f"op={str(active).lower()} monitoring="
                    f"{item.get('monitoringStatus')}")
            if operator_equipment and item['equipmentInstanceId'] != operator_equipment:
                lines.append((f"unit {item['equipmentInstanceId'][:8]}", True,
                              note + ' — info, bukan unit demo'))
                continue
            lines.append((f"unit {item['equipmentInstanceId'][:8]}", active, note))
    return lines, token


def run(transport, username, password, timeout_s=90.0, poll_gap_s=2.0,
        camera_target=None, operator_equipment=None):
    """Loop sampai deadline; return (lines, all_ok)."""
    deadline = time.monotonic() + timeout_s
    best = []
    while True:
        lines, _token = check_once(transport, username, password, poll_gap_s,
                                   camera_target, operator_equipment)
        best = lines
        all_ok = lines and all(ok for _label, ok, _note in lines)
        if all_ok:
            return lines, True
        if time.monotonic() >= deadline:
            return lines, False
        time.sleep(2.0)


def render(lines):
    """Cetak checklist; return jumlah gagal."""
    width = max((len(label) for label, _ok, _note in lines), default=0)
    failed = 0
    for label, ok, note in lines:
        mark = '✓' if ok else '✗'
        if not ok:
            failed += 1
        print(f'  {mark} {label.ljust(width)} — {note}')
    return failed


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Pre-flight readiness HARNES: ukur kesiapan uji end-to-end '
                    '(health, auth, frame mengalir, unit operating).')
    parser.add_argument('--port', type=int,
                        default=int(os.environ.get('HARNES_SERVER_PORT') or 8080),
                        help='port server (default HARNES_SERVER_PORT/8080)')
    parser.add_argument('--timeout', type=float,
                        default=float(os.environ.get('HARNES_PREFLIGHT_TIMEOUT') or 90),
                        help='detik maksimum menunggu kesiapan (default 90)')
    parser.add_argument('--camera', default=os.environ.get('HARNES_DEMO_CAMERA')
                        or 'Site Barat',
                        help='kamera demo yang diadili (default: Site Barat)')
    parser.add_argument('--operator', default=os.environ.get('HARNES_DEMO_OPERATOR')
                        or 'dewi',
                        help='operator demo — unitnya yang menentukan siap/tidak '
                             '(default: dewi)')
    args = parser.parse_args(argv)

    username = os.environ.get('HARNES_HSE_USER') or 'anything'
    password = os.environ.get('HARNES_HSE_PASS') or 'anythingcuy06'
    transport = make_transport(f'http://127.0.0.1:{args.port}')
    print(f'== PRE-FLIGHT HARNES → 127.0.0.1:{args.port} (deadline {args.timeout:g}s)')
    equipment = operator_unit(args.operator)
    if equipment is None:
        print(f"== operator '{args.operator}' tidak punya assignment aktif — "
              'assign dulu lewat app HSE; unit dilewati sebagai gagal ==')
        return 1
    lines, ready = run(transport, username, password, timeout_s=args.timeout,
                       camera_target=args.camera, operator_equipment=equipment)
    failed = render(lines)
    if ready:
        lan_ip = None
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(('192.168.110.114', 1))
            lan_ip = sock.getsockname()[0]
        except OSError:
            pass
        base = f'http://{lan_ip}:{args.port}/' if lan_ip else f'http://<ip-mac>:{args.port}/'
        print('== SIAP UJI — iPhone:', base)
        return 0
    print(f'== BELUM SIAP — {failed} baris gagal (lihat ✗ di atas)')
    return 1


if __name__ == '__main__':
    sys.exit(main())
