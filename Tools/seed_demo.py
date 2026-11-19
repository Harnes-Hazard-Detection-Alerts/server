#!/usr/bin/env python3
"""Seed data — SATU-SATUNYA sumber pembuatan data (sejak 11 Nov 2026).

Dua skenario (keputusan owner 12 Nov 2026):

- dev (default) — testing siap pakai tanpa device sungguhan: akun demo,
  kamera simulasi (opsional, --video-dir), katalog nama equipment, STATUS
  OPERASIONAL (tiap kamera: assignment aktif + sesi login + shift binding
  + START OPERATING — HSE langsung melihat semua excavator "Operating now"),
  dan riwayat alert.
- real — baseline roleplay: akun + kamera + katalog equipment SAJA. Tanpa
  sesi, tanpa shift binding, tanpa assignment, tanpa device, tanpa riwayat
  alert — semua operator un-assigned dan offline; device/online hanya muncul
  saat operator sungguhan login dan register dari app.

Registrasi kamera simulasi pindah dari start-site-server.sh ke sini; script
sh kini murni orkestrasi (download video → migrasi → panggil seed ini →
server nyala). Akun HSE demo yang dulu dibuat manual kini ikut seed.

Sengaja TIDAK menyentuh:
- device_tokens    — didaftarkan app operator sendiri (POST /me/device)

Sesi demo berumur default 7 hari (keputusan owner 14 Nov 2026; override env
HARNES_SEED_SESSION_DAYS) — 12 jam membuat gate model diam-diam mati sehari
setelah reset; re-aktifasi cepat tanpa device lewat menu harnes opsi 5
(perintah `ops-relogin`, Tools/relogin_demo.py). Login operator di device
sungguhan tetap mengambil alih secara eksklusif (aman — kontrak upstream),
shift demo hangus dan device baru yang menyetel ulang.

Idempoten: akun dan katalog nama equipment dilewati bila sudah ada; riwayat
alert hanya ditulis bila tabel masih (hampir) kosong — gunakan --add untuk
menambah batch baru ber-stempel waktu sekarang; assignment/shift hanya
diisi bila belum ada assignment aktif. Seed ini TIDAK menghapus apapun —
pindah skenario secara fresh ditangani Tools/reset_database.py
(full drop + migrasi + seed).

Usage:
    .venv/bin/python Tools/seed_demo.py [--scenario dev|real] [--add] [--video-dir DIR]
"""
import argparse
import os
import psycopg
import random
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Sources.cameras.service import CameraError, create_camera, list_cameras
from Sources.infrastructure.database.connection import connect
from Sources.users.admin import create_account

DEMO_PASSWORD = 'password-demo-2026'
# (username, display name, role, password) — kredensial demo, bukan rahasia.
DEMO_ACCOUNTS = [
    ('anything', 'HSE Site Admin', 'hse', 'anythingcuy06'),
    ('budi', 'Budi Santoso', 'operator', DEMO_PASSWORD),
    ('sari', 'Sari Wulandari', 'operator', DEMO_PASSWORD),
    ('dewi', 'Dewi Anggraini', 'operator', DEMO_PASSWORD),
]
# FPS delivery standar (11 Nov 2026): frame terkirim kini dibatasi 1280px
# (HARNES_FRAME_MAX_WIDTH), jadi sampling bisa lebih rapat dari 4 fps lama
# tanpa membakar MPS/bandwidth — anggaran aman 4 kamera di Mac paket MPS.
FPS_DELIVERY = int(os.environ.get('HARNES_CAMERA_FPS') or '8')
# (awalan file sampel, nama kamera, lokasi) — didaftarkan FPS_DELIVERY, loop.
CAMERA_SAMPLES = [
    ('pexels-tom-fisk', 'Site Timur', 'Area Galian Timur'),
    ('pexels-kelly', 'Site Barat', 'Jalur Akses Barat'),
    ('pexels-ye-qiao', 'Site Depo', 'Halaman Depo'),
]

MESSAGES = {
    'person_entered_zone': 'Person terdeteksi di zona excavator',
    'person_high_risk': 'Person terlalu dekat excavator (high risk)',
    'zone_cleared': 'Zona excavator kembali kosong',
}
HOURS_SPAN = 72


def seed_users():
    created, skipped = [], []
    for username, name, role, password in DEMO_ACCOUNTS:
        with connect() as connection:
            exists = connection.execute(
                'SELECT 1 FROM users WHERE lower(username)=lower(%s)',
                (username,)).fetchone()
        if exists:
            skipped.append(username)
            continue
        create_account(username, name, role, password)
        created.append(f'{username} ({role})')
    return created, skipped



def seed_cameras(video_dir):
    """Daftarkan kamera simulasi dari folder video — idempoten per path file.

    File pertama per awalan (urut nama) dipasangkan ke nama/lokasi tetap
    (FPS_DELIVERY, loop) dengan pola yang dulu hidup di start-site-server.sh;
    kamera dengan file yang sudah terdaftar dilewati (unique cameras.source).
    """
    if not video_dir:
        print('seed kamera: lewati — folder video tidak diberikan (opsi --video-dir).')
        return
    root = Path(video_dir).expanduser().resolve()
    if not root.is_dir():
        print(f'seed kamera: lewati — folder video tidak ditemukan: {root}')
        return
    now_ms = time.time_ns() // 1_000_000
    registered, existing, missing = [], [], []
    # Idempoten per NAMA juga, bukan cuma source-path: folder video bisa
    # berisi >1 file per awalan (mis. rilis lama+baru); kalau hanya source
    # yang dicek, kamera kembar terdaftar tiap ganti file — thread playback
    # dobel menggandakan beban MPS diam-diam.
    known_names = {camera['name'] for camera in list_cameras(connect)}
    for prefix, name, location in CAMERA_SAMPLES:
        match = next((p for p in sorted(root.glob(prefix + '*')) if p.is_file()), None)
        if match is None:
            missing.append(f'{name} (awalan {prefix}*)')
            continue
        if name in known_names:
            existing.append(name)
            continue
        try:
            create_camera(name, location, 'recording', str(match), FPS_DELIVERY, True, now_ms)
            registered.append(name)
        except CameraError as error:
            if error.code != 'already_exists':
                raise
            existing.append(name)
    if registered:
        print(f'Kamera terdaftar: {", ".join(registered)} (fps {FPS_DELIVERY}, loop)')
    if existing:
        print(f'Kamera dilewati (file sudah terdaftar): {", ".join(existing)}')
    if missing:
        print(f'Kamera belum bisa didaftarkan (file tidak ada di {root}): '
              + ', '.join(missing))



def seed_equipment_instances():
    """Nama tampilan untuk fallback equipment stabil milik tiap kamera demo."""
    with connect() as connection:
        cameras = connection.execute(
            'SELECT id, name FROM cameras ORDER BY name, id').fetchall()
        created = []
        for index, (camera_id, _) in enumerate(cameras, start=1):
            equipment_id = f'{str(camera_id)[:8]}-eq-1'
            equipment_name = f'Excavator - {index}'
            inserted = connection.execute(
                'INSERT INTO equipment_instances(equipment_instance_id, name) '
                'VALUES (%s, %s) ON CONFLICT (equipment_instance_id) DO NOTHING '
                'RETURNING equipment_instance_id',
                (equipment_id, equipment_name)).fetchone()
            if inserted:
                created.append(equipment_id)
        return created, len(cameras)


def seed_equipment_camera_links():
    """Link kamera↔unit (dev): HSE langsung melihat semua excavator di registry.

    list_equipment membaca equipment_cameras — tanpa link, daftar unit HSE
    kosong walau assignment terisi. Skenario real TIDAK memanggil fungsi ini:
    menambah unit termasuk alur roleplay HSE lewat app.
    """
    with connect() as connection:
        hse_id = connection.execute(
            "SELECT id FROM users WHERE role='hse' AND is_active "
            'ORDER BY created_at LIMIT 1').fetchone()
        cameras = connection.execute(
            'SELECT id, registered_at_ms FROM cameras ORDER BY name').fetchall()
    linked = 0
    for camera_id, registered_ms in cameras:
        equipment = f'{str(camera_id)[:8]}-eq-1'
        try:
            with connect() as connection:
                connection.execute(
                    'INSERT INTO equipment_cameras(equipment_instance_id, camera_id, '
                    'created_at_ms, created_by) VALUES (%s, %s, %s, %s)',
                    (equipment, camera_id, registered_ms, hse_id[0] if hse_id else None))
                linked += 1
        except psycopg.errors.UniqueViolation:
            pass  # kamera/unit sudah ter-link (mis. dibuat HSE manual) — hormati
    print(f'seed link unit: {linked} kamera↔unit terhubung')


def seed_alerts(add_mode):
    now_ms = time.time_ns() // 1_000_000
    with connect() as connection:
        existing = connection.execute('SELECT count(*) FROM alerts').fetchone()[0]
        if existing and not add_mode:
            return None, existing
        cameras = connection.execute(
            'SELECT id, name FROM cameras ORDER BY name').fetchall()
        operators = [row[0] for row in connection.execute(
            "SELECT id FROM users WHERE role='operator' AND is_active ORDER BY username").fetchall()]
        random.seed(42)  # demo deterministik antar-run

        rows, deliveries = [], []
        for index, (camera_id, camera_name) in enumerate(cameras):
            equipment = f'{str(camera_id)[:8]}-eq-1'
            # 5 insiden per kamera tersebar 72 jam: entered → (high risk?) → cleared.
            for incident in range(5):
                base_ms = now_ms - random.randint(1, HOURS_SPAN * 3600) * 1000
                persons = random.randint(1, 3)
                entered_ms = base_ms
                cleared_ms = base_ms + random.randint(25, 180) * 1000
                rows.append(('person_entered_zone', MESSAGES['person_entered_zone'],
                             camera_id, equipment, persons, entered_ms,
                             'resolved', cleared_ms, entered_ms))
                if incident % 2 == 0:  # separuh insiden sempat jadi high-risk
                    high_ms = entered_ms + random.randint(5, 20) * 1000
                    rows.append(('person_high_risk', MESSAGES['person_high_risk'],
                                 camera_id, equipment, 1, high_ms,
                                 'resolved', cleared_ms, high_ms))
                rows.append(('zone_cleared', MESSAGES['zone_cleared'],
                             camera_id, equipment, 0, cleared_ms,
                             'resolved', cleared_ms, cleared_ms))
                # Delivery audit kanal live per alert person (operator silih-ganti).
                for kind_ms in (entered_ms, cleared_ms):
                    if operators:
                        operator_id = operators[(incident + index) % len(operators)]
                        deliveries.append((kind_ms, operator_id))

        inserted = 0
        for (kind, message, camera_id, equipment, persons, occurred,
             status, resolved_ms, created_ms) in rows:
            alert_id = connection.execute(
                'INSERT INTO alerts(kind, message, camera_id, equipment_instance_id, '
                'persons_in_zone, occurred_at_ms, status, resolved_at_ms, created_at_ms) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                (kind, message, camera_id, equipment, persons, occurred,
                 status, resolved_ms, created_ms)).fetchone()[0]
            inserted += 1
            for (kind_ms, operator_id) in deliveries:
                if abs(kind_ms - occurred) < 1000 and operator_id:
                    connection.execute(
                        'INSERT INTO alert_deliveries(alert_id, operator_id, target, '
                        'status, at_ms, channel) VALUES (%s,%s,%s,%s,%s,%s)',
                        (alert_id, operator_id, 'iphone', 'sent',
                         kind_ms + 2000, 'live'))
        return inserted, existing + inserted


def _seed_session_days():
    """Umur sesi demo dalam HARI (keputusan owner 14 Nov 2026).

    Default 7: sesi 12 jam membuat gate "model jalan hanya saat unit
    operating" diam-diam mati sehari setelah reset dev — keluhan lapangan
    14 Nov. Draftripsi user: env HARNES_SEED_SESSION_DAYS menimpa (min 1).
    """
    try:
        return max(1, int(os.environ.get('HARNES_SEED_SESSION_DAYS') or '7'))
    except ValueError:
        return 7


def seed_operational_state():
    """Status demo siap-pakai: tiap kamera punya operator aktif ber-shift
    dan SUDAH Start Operating.

    Assignment per (kamera, operator) dipasangkan satu-satu (ORDER BY nama);
    equipment id mengikuti pola fallback evaluator "{8-char id kamera}-eq-1"
    supaya event/alert kamera otomatis berpasangan dengan unit di app.
    Sesi + shift binding + operation_started dibuat ulang hanya bila belum
    ada yang hidup — device operator sungguhan tetap boleh ambil alih
    kapan pun (login eksklusif upstream); setelah itu seed tidak akan
    menimpanya. Skenario real tidak memanggil fungsi ini sama sekali.
    """
    import psycopg
    from Sources.assignments.service import AssignmentError, create_assignment
    from Sources.auth.service import token_hash

    with connect() as connection:
        cameras = connection.execute('SELECT id FROM cameras ORDER BY name').fetchall()
        operators = connection.execute(
            "SELECT id FROM users WHERE role='operator' AND is_active "
            "AND lower(username) IN ('budi','sari','dewi') ORDER BY username").fetchall()
    if not cameras or not operators:
        print('seed ops: kamera/operator demo tidak lengkap — lewati')
        return
    now_ms = time.time_ns() // 1_000_000
    paired = 0
    for (camera_id,), (operator_id,) in zip(cameras, operators):
        equipment = f'{str(camera_id)[:8]}-eq-1'
        try:
            item = create_assignment(equipment, operator_id, now_ms)
            fresh = bool(item.get('created'))
        except AssignmentError:
            fresh = False  # sudah dipegang/di-unassign HSE manual — hormati state
        if not fresh:
            continue  # pasangan LAMA tidak disentuh: logout operator dihormati,
                      # begitu pula assignment yang diubah HSE setelah seed awal
        with connect() as connection:
            live_session = connection.execute(
                'SELECT 1 FROM sessions WHERE user_id=%s AND expires_at>now()',
                (operator_id,)).fetchone()
            if live_session is None:
                connection.execute(
                    "INSERT INTO sessions(token_hash,user_id,expires_at) "
                    "VALUES (%s,%s,now() + %s * interval '1 day')",
                    (token_hash(secrets.token_urlsafe(32)), operator_id,
                     _seed_session_days()))
            # Semantik sama dengan PUT /me/shift: ambil-alih equipment dari
            # binding yang sesinya habis, lalu pasang binding baru operator ini.
            connection.execute(
                'DELETE FROM shift_bindings b WHERE b.equipment_instance_id=%s '
                'AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.user_id=b.operator_id '
                'AND s.expires_at>now())', (equipment,))
            connection.execute(
                'DELETE FROM shift_bindings WHERE operator_id=%s', (operator_id,))
            try:
                connection.execute(
                    'INSERT INTO shift_bindings(operator_id, equipment_instance_id, '
                    'set_at_ms, operation_started_at_ms) VALUES (%s, %s, %s, %s)',
                    (operator_id, equipment, now_ms, now_ms))
                paired += 1
            except psycopg.errors.UniqueViolation:
                pass  # binding aktif milik device lain sedang memegang — biarkan
    print(f'seed ops: {paired}/{len(cameras)} pasangan baru diaktifkan (sudah Start Operating)')


DEMO_SOLO_CAMERA = 'Site Barat'          # pintu demo tunggal (owner 15 Nov)
DEMO_SOLO_RTSP = 'rtsp://127.0.0.1:8554/site-barat'
DEMO_SOLO_OPERATOR = 'dewi'


def seed_solo_camera():
    """Skenario demo: SATU kamera RTSP (Site Barat), desired ON.

    Produser start-video-rtsp.sh site-barat harus jalan agar frame hidup;
    desired_on=true membuat supervisor menjaga playback tetap nyala."""
    now_ms = time.time_ns() // 1_000_000
    known = {camera['name'] for camera in list_cameras(connect)}
    if DEMO_SOLO_CAMERA in known:
        print(f'kamera solo: {DEMO_SOLO_CAMERA} sudah terdaftar — dilewati')
        return
    try:
        create_camera(DEMO_SOLO_CAMERA, 'Jalur Akses Barat', 'rtsp',
                      DEMO_SOLO_RTSP, FPS_DELIVERY, True, now_ms)
    except CameraError as error:
        if error.code != 'already_exists':
            raise
        print(f'kamera solo: sumber sudah terdaftar — dilewati')
        return
    with connect() as connection:
        connection.execute('UPDATE cameras SET desired_on=true '
                           'WHERE name=%s', (DEMO_SOLO_CAMERA,))
    print(f'kamera solo terdaftar: {DEMO_SOLO_CAMERA} ({DEMO_SOLO_RTSP}, desired ON)')


def seed_operational_state_solo():
    """Skenario demo: unit TUNGGAL milik kamera solo di-assign ke operator
    demo pilihan (dewi) dan langsung Start Operating — feed aman terlihat
    di iOS tanpa menunggu roleplay manual. Sesi hidup HARNES_SEED_SESSION_DAYS."""
    import psycopg
    from Sources.assignments.service import AssignmentError, create_assignment
    from Sources.auth.service import token_hash

    with connect() as connection:
        camera = connection.execute('SELECT id FROM cameras WHERE name=%s',
                                     (DEMO_SOLO_CAMERA,)).fetchone()
        operator = connection.execute(
            "SELECT id FROM users WHERE role='operator' AND is_active "
            'AND lower(username)=%s', (DEMO_SOLO_OPERATOR.lower(),)).fetchone()
    if not camera or not operator:
        print('seed solo ops: kamera/operator demo tidak ditemukan — lewati')
        return
    camera_id, operator_id = camera[0], operator[0]
    equipment = f'{str(camera_id)[:8]}-eq-1'
    now_ms = time.time_ns() // 1_000_000
    try:
        item = create_assignment(equipment, operator_id, now_ms)
        if not item.get('created'):
            print(f'seed solo ops: assignment {DEMO_SOLO_OPERATOR}/{equipment} '
                  'sudah ada — hanya diperbarui status operating bila kosong')
    except AssignmentError as error:
        print(f'seed solo ops: assignment ditolak ({error.code}) — cek manual lewat app')
        return
    with connect() as connection:
        live = connection.execute('SELECT 1 FROM sessions WHERE user_id=%s '
                                   'AND expires_at>now()', (operator_id,)).fetchone()
        if live is None:
            connection.execute("INSERT INTO sessions(token_hash,user_id,expires_at) "
                               "VALUES (%s,%s,now() + %s * interval '1 day')",
                               (token_hash(secrets.token_urlsafe(32)), operator_id,
                                _seed_session_days()))
        connection.execute('DELETE FROM shift_bindings b WHERE b.equipment_instance_id=%s '
                           'AND NOT EXISTS (SELECT 1 FROM sessions s WHERE '
                           's.user_id=b.operator_id AND s.expires_at>now())', (equipment,))
        connection.execute('DELETE FROM shift_bindings WHERE operator_id=%s', (operator_id,))
        connection.execute('INSERT INTO shift_bindings(operator_id, equipment_instance_id, '
                           'set_at_ms, operation_started_at_ms) VALUES (%s, %s, %s, %s)',
                           (operator_id, equipment, now_ms, now_ms))
    print(f"seed solo ops: unit {equipment} -> {DEMO_SOLO_OPERATOR} AKTIF dan Start Operating")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Seed data — satu-satunya sumber data (akun, kamera, '
                    'katalog equipment, status ops dev, alert).')
    parser.add_argument('--scenario', choices=['dev', 'real', 'demo'], default='dev',
                        help='dev: ops+alert terisi (testing tanpa device). '
                             'real: hanya akun — alur roleplay penuh lewat app. '
                             'demo: SATU kamera RTSP Site Barat + dewi Start '
                             'Operating (pintu uji tunggal, owner 15 Nov).')
    parser.add_argument('--add', action='store_true',
                        help='tambah batch alert baru walau tabel sudah berisi')
    parser.add_argument('--video-dir', default=None,
                        help='folder video sampel: kamera simulasi didaftarkan '
                             'dari sini (default: lewati kamera)')
    args = parser.parse_args(argv)
    created, skipped = seed_users()
    if created:
        print(f'Akun dibuat: {", ".join(created)} — password demo operator: '
              f'{DEMO_PASSWORD}; HSE demo: anything/anythingcuy06')
    if skipped:
        print(f'Akun dilewati (sudah ada): {", ".join(skipped)}')
    if args.scenario == 'demo':
        seed_solo_camera()
        equipment_created, equipment_total = seed_equipment_instances()
        seed_equipment_camera_links()
        seed_operational_state_solo()
        print('== Skenario DEMO siap ==')
        print(f'Satu kamera ({DEMO_SOLO_CAMERA}) + satu unit untuk '
              f'{DEMO_SOLO_OPERATOR} (Start Operating), tanpa riwayat alert.')
        print('Pastikan produser jalan: sh Scripts/harnes.sh d (demo-ready) '
              'atau start-video-rtsp.sh site-barat.')
        return
    if args.scenario == 'real':
        if args.video_dir:
            print('Folder video diabaikan di skenario real — kamera ditambahkan '
                  'HSE lewat app (input RTSP/rekaman).')
        print('Skenario real: HANYA akun. Tanpa kamera, unit, sesi, shift '
              'binding, assignment, device, dan riwayat alert.')
        print('Semua operator belum ter-assign dan offline. Alur penuh lewat '
              'app: HSE login → tambah kamera (RTSP) → tambah unit dari kamera '
              '→ assign operator → operator login → Start Operating → alert '
              'terproses.')
        return
    seed_cameras(args.video_dir)          # kamera dulu — dibutuhkan katalog nama
    equipment_created, equipment_total = seed_equipment_instances()
    print(
        f'Equipment bernama dibuat: {len(equipment_created)} '
        f'(kamera demo: {equipment_total}).'
    )
    seed_equipment_camera_links()
    seed_operational_state()
    inserted, total = seed_alerts(args.add)
    if inserted is None:
        print(f'Alert dilewati — tabel sudah berisi {total} baris (pakai --add untuk menambah).')
    else:
        print(f'Alert terisi: {inserted} baris (total kini {total}).')


if __name__ == '__main__':
    main()
