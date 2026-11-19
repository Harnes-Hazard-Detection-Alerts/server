#!/usr/bin/env python3
"""Reset database ke skenario dev atau real — full drop + migrasi + seed.

Satu-satunya alat reset database (keputusan owner 12 Nov 2026). Setiap
pemanggilan = database FRESH: semua tabel schema public di-drop (termasuk
schema_migrations), migrasi dijalankan ulang dari file pertama, lalu
seed sesuai skenario lewat Tools/seed_demo.py:

- dev  → seed penuh: akun demo, kamera (bila --video-dir diberikan),
  katalog equipment + link unit, tiap kamera operating (assignment + sesi
  + shift binding + Start Operating), dan riwayat alert. Testing siap
  pakai tanpa device sungguhan.
- real → HANYA akun: tanpa kamera dan unit (ditambahkan HSE via app —
  input RTSP/rekaman disimpan di cameras.source lalu diproses server),
  tanpa sesi, shift, assignment, device, dan riwayat — semua operator
  un-assigned dan offline, alur penuh dikerjakan lewat app (roleplay).

TIDAK menyentuh role PostgreSQL, database itu sendiri, pg_hba.conf, atau
.env — target ikuti DATABASE_URL (proses env atau Server/.env) dan
dilibat sebelum drop. Server harus dimatikan dulu; skrip sh memeriksa
pgrep, cek pg_stat_activity di sini lapis kedua.

Usage:
    .venv/bin/python Tools/reset_database.py --scenario dev|real [--video-dir DIR] [--start]
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from Sources.infrastructure.database.connection import connect, database_url
from Sources.infrastructure.database.migrate import migrate

import seed_demo


def drop_all_tables(connection):
    """Drop setiap tabel schema public — database kembali kosong total."""
    names = [row[0] for row in connection.execute(
        'SELECT tablename FROM pg_tables WHERE schemaname = %s',
        ('public',)).fetchall()]
    for name in names:
        connection.execute(
            sql.SQL('DROP TABLE IF EXISTS {}.{} CASCADE').format(
                sql.Identifier('public'), sql.Identifier(name)))
    return names


def drop_all_functions(connection):
    """Drop setiap function schema public.

    CREATE FUNCTION di migrasi (mis. 001 set_user_updated_at) tidak ikut
    ter-drop bersama tabelnya; tanpa ini migrasi ulang gagal DuplicateFunction.
    Trigger definition mati bersama tabelnya, tinggal function yang menetap.
    """
    rows = connection.execute(
        'SELECT p.proname, pg_get_function_identity_arguments(p.oid) '
        'FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace '
        "WHERE n.nspname = 'public'").fetchall()
    for proname, arguments in rows:
        target = sql.SQL('public.{}').format(sql.Identifier(proname))
        target = (sql.SQL('{}({})').format(target, sql.SQL(arguments))
                  if arguments.strip() else sql.SQL('{}()').format(target))
        connection.execute(sql.SQL('DROP FUNCTION IF EXISTS {}').format(target))
    return [(row[0]) for row in rows]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Reset FULL database ke skenario dev atau real '
                    '(drop semua tabel → migrasi ulang → seed).')
    parser.add_argument('--scenario', choices=['dev', 'real', 'demo'],
                        required=True,
                        help='dev: ops + riwayat terisi (testing tanpa '
                             'device). real: hanya akun — roleplay penuh '
                             'lewat app. demo: SATU kamera RTSP Site Barat '
                             '+ operator dewi Start Operating (pintu uji '
                             'tunggal; butuh produser site-barat jalan).')
    parser.add_argument('--video-dir', default=None,
                        help='folder video sampel untuk kamera simulasi; '
                             'tanpa ini kamera tidak didaftarkan.')
    parser.add_argument('--start', action='store_true',
                        help='setelah reset+seed, langsung nyalakan server via '
                             'Scripts/start-site-server.sh (auto-start kamera; '
                             'skenario seed disinkronkan lewat HARNES_SEED_SCENARIO).')
    args = parser.parse_args(argv)

    target = conninfo_to_dict(database_url())
    print(f"Target reset: database '{target.get('dbname')}' @ "
          f"{target.get('host')} (user {target.get('user')}).")
    print('SEMUA tabel domain akan di-drop lalu migrasi dijalankan dari awal.')

    with connect() as connection:
        busy = connection.execute(
            'SELECT count(*) FROM pg_stat_activity '
            'WHERE datname = current_database() AND pid <> pg_backend_pid()'
        ).fetchone()[0]
        if busy:
            raise SystemExit(
                f'Masih ada {busy} koneksi lain ke database ini. '
                'Matikan server dulu: pkill -f Sources.main '
                '(atau Ctrl+C di terminal server), lalu ulangi reset.')
        dropped = drop_all_tables(connection)
        functions = drop_all_functions(connection)
        migrated = migrate(connection)
    print(f'Tabel di-drop: {len(dropped)} · function di-drop: {len(functions)} · '
          f'migrasi baru: {migrated} (skema segar dari file pertama).')

    seed_arguments = ['--scenario', args.scenario]
    if args.video_dir:
        if args.scenario == 'real':
            print('--video-dir diabaikan di skenario real — kamera ditambahkan '
                  'HSE lewat app (input RTSP/rekaman).')
        else:
            seed_arguments += ['--video-dir', args.video_dir]
    seed_demo.main(seed_arguments)

    if args.scenario == 'demo':
        print('== Skenario DEMO siap ==')
        print('SATU kamera (Site Barat, RTSP) + unit untuk dewi Start '
              'Operating — tanpa kamera/unit/sesi/alert lain.')
        print('Feed tersedia begitu produser jalan: sh Scripts/harnes.sh d, '
              'atau start-video-rtsp.sh site-barat lalu server opsi 1.')
    elif args.scenario == 'real':
        print('== Skenario REAL siap ==')
        print('Semua operator: belum ter-assign, tidak login, tanpa device — offline.')
        print('Belum ada kamera/unit — ditambahkan HSE lewat app: kamera RTSP '
              'disimpan di server lalu diproses (start/auto-start + fallback).')
        print('Alur roleplay: HSE login → tambah kamera (RTSP) → tambah unit '
              'dari kamera → assign operator → operator login → Start '
              'Operating → alert terproses.')
    else:
        print('== Skenario DEV siap ==')
        print('Semua kamera ter-link sebagai unit operating dengan operator demo; '
              'daftar unit HSE terisi, riwayat alert terisi.')
        print('Login demo — HSE: anything/anythingcuy06 · operator: '
              'budi|sari|dewi (password-demo-2026).')
    if args.start:
        start_script = Path(__file__).resolve().parents[2] / 'Scripts' / 'start-site-server.sh'
        if not start_script.is_file():
            raise SystemExit(f'Skrip server tidak ditemukan: {start_script}')
        print(f'--start: menyalakan server ({start_script.name}) '
              f'dengan skenario seed {args.scenario}…')
        os.environ['HARNES_SEED_SCENARIO'] = args.scenario
        if args.video_dir:
            os.environ.setdefault('HARNES_VIDEO_DIR', args.video_dir)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv('/bin/sh',
                 ['sh', str(start_script)])
        return


if __name__ == '__main__':
    try:
        main()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except psycopg.Error:
        raise SystemExit('Reset gagal; transaksi dibatalkan dan data yang '
                         'belum di-drop tetap utuh. Periksa koneksi database '
                         'lalu ulangi.') from None
