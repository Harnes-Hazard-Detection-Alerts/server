"""CLI registrasi kamera (tanpa HTTP) — cara utama memasukkan video sebagai CCTV.

Penggunaan (dari AppleApp/Server, DB lokal harus sudah dimigrasi):

    .venv/bin/python -m Sources.cameras.cli add /path/rekaman.mp4 \\
        --name "Site Timur" --location "Main Entrance" --fps 8 --loop
    .venv/bin/python -m Sources.cameras.cli list
    .venv/bin/python -m Sources.cameras.cli remove <id-atau-prefiks>

Video tetap disimpan DI LUAR repo (hanya pathnya yang dicatat). Start/stop
playback lewat server: `--auto-start-cameras` saat boot, atau endpoint
POST /cameras/{id}/start | /stop (sesi HSE) — CLI tidak menyentuh
proses server yang sedang jalan.
"""
import argparse
import sys
import time

from .service import CameraError, create_camera, delete_camera, list_cameras
from ..infrastructure.database.connection import connect


def _now_ms():
    return time.time_ns() // 1_000_000


def main():
    parser = argparse.ArgumentParser(description='Registrasi kamera (CCTV simulasi / RTSP live).')
    commands = parser.add_subparsers(dest='command', required=True)

    add = commands.add_parser('add', help='daftarkan kamera: file video ATAU URL rtsp://')
    add.add_argument('source', help='path file video di Mac, atau URL rtsp://produser:8554/...')
    add.add_argument('--name', required=True, help='nama kamera, 1-100 karakter')
    add.add_argument('--location', default=None, help='label lokasi site, 1-100 karakter (opsional)')
    add.add_argument('--fps', type=int, default=None, help='sampling frame per detik 1-60 (opsional)')
    add.add_argument('--loop', action='store_true', help='(recording) ulangi dari awal setelah selesai')

    commands.add_parser('list', help='daftar kamera terdaftar')
    remove = commands.add_parser('remove', help='hapus kamera berdasarkan id (atau prefiks unik)')
    remove.add_argument('id')

    args = parser.parse_args()
    try:
        if args.command == 'add':
            source = args.source.strip()
            if source.startswith('rtsp://'):
                camera = create_camera(args.name.strip(), args.location, 'rtsp',
                                       source, args.fps, True, _now_ms(), connect)
            else:
                camera = create_camera(args.name.strip(), args.location, 'recording',
                                       source, args.fps, args.loop, _now_ms(), connect)
            print(f"Kamera terdaftar: {camera['name']}"
                  + (f" — {camera['location']}" if camera['location'] else '')
                  + f" (id={camera['id']}, type={camera['type']}, fps={camera['playbackFps'] or 'semua'})")
            print('Nyalakan lewat server:  ... --auto-start-cameras   atau   POST /cameras/' + camera['id'] + '/start')
        elif args.command == 'list':
            cameras = list_cameras(connect)
            if not cameras:
                print('Belum ada kamera. Tambahkan dengan perintah `add`.')
            for camera in cameras:
                print(f"{camera['id']}  {camera['status']:<8} {camera['name']:<28} "
                      f"{camera['location'] or '-':<20} {camera['source']}")
        elif args.command == 'remove':
            target = _resolve(args.id)
            removed = delete_camera(target, connect)
            if removed is None:
                raise SystemExit(f'kamera {args.id} tidak ditemukan.')
            print(f'Kamera dihapus: {removed}')
    except CameraError as error:
        messages = {'file_not_found': 'File video tidak ditemukan — periksa path.',
                    'invalid_source': 'Kamera rtsp butuh URL yang diawali rtsp://',
                   'already_exists': 'Kamera dengan file itu sudah terdaftar.'}
        raise SystemExit(messages.get(error.code, f'gagal: {error.code}')) from None
    except Exception:
        raise SystemExit('Gagal mengakses database. Jalankan setup-database.sh dan migrasi dulu.') from None


def _resolve(identifier):
    """Terima id penuh atau prefiks unik (kenyamanan terminal)."""
    if len(identifier) == 36:
        return identifier
    matches = [c for c in list_cameras(connect) if c['id'].startswith(identifier)]
    if len(matches) == 1:
        return matches[0]['id']
    if not matches:
        raise SystemExit(f'kamera {identifier} tidak ditemukan.')
    raise SystemExit(f'prefiks ambigu ({len(matches)} kandidat) — gunakan id penuh.')


if __name__ == '__main__':
    main()
