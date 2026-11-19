#!/bin/sh
# Reset FULL database ke skenario — bolak-balik kapan pun (keputusan owner
# 12 Nov 2026). Setiap pemanggilan: SEMUA tabel domain di-drop → migrasi
# dijalankan ulang → seed sesuai skenario (Server/Tools/reset_database.py
# + seed_demo.py adalah satu-satunya sumber).
#
#   dev  → testing siap pakai tanpa device: akun demo, kamera dari folder
#          video, semua kamera operating (assignment + sesi + Start),
#          riwayat alert terisi.
#   demo → pintu uji tunggal: SATU kamera RTSP Site Depo (Ye Qiao) + unit untuk
#          operator dewi (Start Operating), tanpa kamera/unit/sesi/alert
#          lain. Butuh produser site-depo jalan (harnes Demo / start-video).
#   real → baseline roleplay: HANYA akun — kamera/unit TIDAK diseed;
#          HSE menambahkan manual lewat app (input RTSP disimpan di
#          cameras.source lalu diproses server) sebelum assign operator.
#          Alias: prod.
#
# Server harus dimatikan dulu — skrip ini memeriksa dan menolak bila masih
# ada proses Sources.main. Persis seperti start-site-server.sh, kamera
# diambil dari recordings/ (atau HARNES_VIDEO_DIR / --video-dir).
#
# Usage:
#   sh Scripts/reset-database.sh dev
#   sh Scripts/reset-database.sh demo
#   sh Scripts/reset-database.sh real
#   sh Scripts/reset-database.sh dev --start   # reset + langsung
#                                  nyalakan server (auto-start kamera, alert hidup)
#   sh Scripts/reset-database.sh real --start  # server real tanpa re-seed dev
#   HARNES_VIDEO_DIR=/lain sh Scripts/reset-database.sh dev
set -eu

usage() {
    echo "Usage: sh Scripts/reset-database.sh dev|demo|real [--video-dir DIR] [--start]" >&2
    exit 1
}

[ "$#" -ge 1 ] || usage
SCENARIO="$1"
case "$SCENARIO" in
    dev|demo) ;;
    real|prod) SCENARIO=real ;;
    *) usage ;;
esac
shift

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

if [ ! -x .venv/bin/python ]; then
    echo 'Belum ada .venv — jalankan dulu: sh Scripts/setup-database.sh' >&2
    exit 1
fi
if pgrep -f 'Sources.main' >/dev/null 2>&1; then
    echo 'Server masih berjalan. Reset ditolak.' >&2
    echo 'Matikan dulu: pkill -f Sources.main (atau Ctrl+C di terminal server), lalu ulangi.' >&2
    exit 1
fi

REC_DIR="$(pwd)/recordings"
VIDEO_DIR="${HARNES_VIDEO_DIR:-$REC_DIR}"
if [ "$SCENARIO" = "dev" ]; then
    case " $* " in
        *" --video-dir "*|*" --video-dir="*)
            exec .venv/bin/python Tools/reset_database.py --scenario "$SCENARIO" "$@" ;;
        *)  exec .venv/bin/python Tools/reset_database.py --scenario "$SCENARIO" \
                --video-dir "$VIDEO_DIR" "$@" ;;
    esac
else
    # real/demo: kamera TIDAK diseed dari folder video — real ditambahkan HSE
    # lewat app; demo mendaftarkan SATU kamera RTSP Site Depo / Ye Qiao sendiri.
    exec .venv/bin/python Tools/reset_database.py --scenario "$SCENARIO" "$@"
fi
