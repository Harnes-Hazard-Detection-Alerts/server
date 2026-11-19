#!/usr/bin/env python3
"""Re-login simulasi operator demo — panggilan menu harnes (opsi 5 / perintah
langsung `ops-relogin`). Satu-satunya alat menghidupkan kembali status
operating tanpa device: keputusan owner 14 Nov 2026.

Latar: gate model server (hemat MPS 13 Nov) hanya menjalankan detector saat
unit punya operator operating = assignment aktif + sesi hidup + Start
Operating. Sesi demo seed suatu saat habis → kamera jalan tapi
"model idle — tidak ada unit operating". Tool ini mengulang ROLEPLAY ASLI
tiap operator ter-assignment lewat HTTP (login → PUT /me/shift →
PUT /me/operation), jadi cache operating di proses server ikut segar tanpa
restart server dan jalur yang dilatih sama persis dengan app.

Sumber daya: daftar operator = assignment aktif di database target env
DATABASE_URL; password demo diambil dari seed_demo.DEMO_PASSWORD — operator
yang password-nya sudah diganti HSE dilewati (pesan: roleplay manual dari
app). Keluaran: ringkasan per operator; exit code 1 bila tidak ada satu pun
yang berhasil (server mati / password berubah).
"""
import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))        # Sources
sys.path.insert(0, str(Path(__file__).resolve().parent))           # seed_demo

from Sources.infrastructure.database.connection import connect       # noqa: E402
from seed_demo import DEMO_PASSWORD                                  # noqa: E402


def _http(base_url, method, path, body=None, token=None):
    """(status, json|None) — request kecil; semua kegagalan jaringan HTTP 0.

    Termasuk RemoteDisconnected (server baru bangun, koneksi putus di tengah
    request — subkelas HTTPException, bukan URLError) supaya boot-server
    tidak meledakkan traceback; caller menilai status 0 sebagai 'coba lagi'."""
    request = urllib.request.Request(
        base_url + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json',
                 **({'Authorization': 'Bearer ' + token} if token else {})})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        return 0, None


def relogin(base_url='http://127.0.0.1:8080', password=DEMO_PASSWORD,
            connection_factory=connect, operator=None):
    """Ulangi login → shift → Start Operating operator ter-assign.

    `operator=None` → SEMUA operator ter-assign (menu opsi 5 / demo lama);
    `operator='dewi'` → hanya itu — pintu demo tunggal dewi×Site Barat
    (owner 14 Nov; harnes demo memanggil dengan --operator dewi).
    Return list laporan per operator: {operator, equipment, login, shift,
    operating, note?} — boolean per langkah supaya pemanggil (menu/CI)
    bisa memutuskan exit code tanpa parsing teks.
    """
    query = ("SELECT u.username, a.equipment_instance_id "
             "FROM assignments a JOIN users u ON u.id = a.operator_id "
             "WHERE a.status='active' AND u.role='operator' AND u.is_active")
    params = None
    if operator:
        query += " AND u.username=%s"
        params = (operator,)
    query += " ORDER BY u.username"
    with connection_factory() as connection:
        rows = connection.execute(query, params).fetchall()
    report = []
    for username, equipment in rows:
        entry = {'operator': username, 'equipment': equipment,
                 'login': False, 'shift': False, 'operating': False}
        status, body = _http(base_url, 'POST', '/auth/login',
                             {'username': username, 'password': password})
        if status != 200:
            entry['note'] = (f'server tidak terjangkau ({status})'
                             if status == 0 else
                             'login gagal — password bukan demo (roleplay manual dari app)')
            report.append(entry)
            print(f'{username}: {entry["note"]}')
            continue
        entry['login'] = True
        token = (body or {}).get('access_token')
        status, _ = _http(base_url, 'PUT', '/me/shift',
                          {'onShiftFor': [equipment]}, token)
        entry['shift'] = status == 200
        if not entry['shift']:
            entry['note'] = f'PUT /me/shift HTTP {status}'
            report.append(entry)
            print(f'{username}: shift gagal (HTTP {status}) — cek assignment {equipment}')
            continue
        status, _ = _http(base_url, 'PUT', '/me/operation', {'active': True}, token)
        entry['operating'] = status == 200
        if not entry['operating']:
            entry['note'] = f'PUT /me/operation HTTP {status}'
        print(f'{username} → {equipment}: login+shift+operating '
              f'{"OK" if entry["operating"] else "GAGAL (operation HTTP " + str(status) + ")"}')
        report.append(entry)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Re-login simulasi operator demo: hidupkan kembali '
                    'operating gate tanpa device (login → shift → Start).')
    parser.add_argument('--port', type=int,
                        default=int(os.environ.get('HARNES_SERVER_PORT') or 8080),
                        help='port HTTP server (default HARNES_SERVER_PORT atau 8080)')
    parser.add_argument('--operator',
                        default=os.environ.get('HARNES_DEMO_OPERATOR'),
                        help='batasi ke satu operator (default: semua operator '
                             'ter-assign; harnes demo memanggil --operator dewi)')
    args = parser.parse_args(argv)
    print(f'== Re-login simulasi operator → http://127.0.0.1:{args.port} ==')
    # Boot server (muat model ±15 dtk + watchdog hidupkan ulang pasca-crash GPU)
    # bisa memutus koneksi di tengah request — relogin idempoten, cukup dicoba
    # ulang sampai stabil; menyerah hanya bila server tak kunjung siap.
    report, attempts = [], 0
    while attempts < 6:
        attempts += 1
        report = relogin(f'http://127.0.0.1:{args.port}',
                        operator=args.operator)
        if report and all(item['operating'] for item in report):
            break
        if attempts < 6:
            print(f'   (server belum stabil — ulang {attempts}/6 dalam 3 dtk)')
            time.sleep(3)
    ok = sum(1 for item in report if item['operating'])
    print(f'== Selesai: {ok}/{len(report)} operator operating ==')
    if not report:
        print('Tidak ada operator ter-assignment — assign dulu dari app HSE.')
        return 1
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
