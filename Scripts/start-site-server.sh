#!/bin/sh
# MURNI START SERVER HARNES — satu command paten untuk semua client:
#   - Simulator Xcode / Mac sendiri : http://localhost:8080/
#   - iPhone fisik di LAN (Wi-Fi sama): http://<nama-mac>.local:8080/
# Bind default 0.0.0.0 = dengarkan semua interface (syarat iPhone fisik bisa
# menembak server; simulator otomatis ikut). Persempit kembali ke loopback:
#   HARNES_SERVER_HOST=127.0.0.1 sh Scripts/start-site-server.sh
#
# TANPA seed, TANPA download video — semuanya jadi tanggung jawab skenario:
# sh Scripts/reset-database.sh dev|real|prod  (drop + migrasi + seed).
# Script ini hanya menjalankan migrasi skema idempoten (no-op bila sudah
# terbaru) lalu menyalakan server, supaya server selalu hidup dengan skema
# valid. Seed saat start JALAN HANYA BILA diminta eksplisit via env:
#   HARNES_SEED_SCENARIO=dev sh ...   # + kamera simulasi → butuh video sampel
#                                    #   di recordings/ (auto-download lewat gh bila
#                                    #   kurang; atur folder via HARNES_VIDEO_DIR)
#   HARNES_SEED_SCENARIO=real sh ... # alias: prod — akun saja, tanpa kamera simulasi
#
# Port: HARNES_SERVER_PORT (default 8080). Watchdog restart saat crash GPU:
# default AKTIF — matikan dengan HARNES_SERVER_WATCH=0.
# Kamera: TIDAK auto-start — nyalakan per kamera dari app HSE; opt-in semua
# sekaligus via HARNES_AUTO_START_CAMERAS=1.
set -eu
# Satu Ctrl+C = berhenti TOTAL, tanpa restart (13 Nov, insiden lapangan):
# shell WAJIB punya handler INT eksplisit — bash sh-mode "menunggu anak dulu"
# lalu python keluar bersih (code 0) dianggap sehat → watchdog bangkit lagi
# dan user terjantung tanpa bisa keluar. Trap = jaminan berhenti seketika.
trap 'exit' INT
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

PORT="${HARNES_SERVER_PORT:-8080}"
HOST="${HARNES_SERVER_HOST:-0.0.0.0}"
AUTOCAM="${HARNES_AUTO_START_CAMERAS:-0}"

if [ ! -x .venv/bin/python ]; then
    echo 'Belum ada .venv — jalankan dulu: sh Scripts/setup-database.sh' >&2
    exit 1
fi
if ! .venv/bin/python -c 'import psycopg' 2>/dev/null; then
    echo 'Dependensi belum lengkap — jalankan dulu: sh Scripts/setup-database.sh' >&2
    exit 1
fi

echo '== Migrasi skema (idempoten — tidak menulis data) =='
.venv/bin/python -m Sources.infrastructure.database.migrate

# Seed hanya bila eksplisit (HARNES_SEED_SCENARIO). Default jalur: tanpa seed.
if [ -n "${HARNES_SEED_SCENARIO:-}" ]; then
    SCENARIO="$HARNES_SEED_SCENARIO"
    case "$SCENARIO" in
        dev) ;;
        real|prod) SCENARIO=real ;;
        *) echo "HARNES_SEED_SCENARIO tidak valid: '$SCENARIO' (harus dev|real|prod)." >&2
           exit 1 ;;
    esac

    REC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/recordings"
    VIDEO_DIR="${HARNES_VIDEO_DIR:-$REC_DIR}"
    # Absolutkan path override supaya path tersimpan di DB selalu absolut
    case "$VIDEO_DIR" in
        /*) ;;
        *)  VIDEO_DIR="$(cd "$VIDEO_DIR" 2>/dev/null && pwd)" || {
                echo "Folder video tidak ditemukan: $VIDEO_DIR" >&2; exit 1; } ;;
    esac

    # Skenario dev memutar file rekaman — unduh sampel bila kurang (sekali saja;
    # tanpa gh, unduh manual: https://github.com/akhnafal-aban/HARNES-Server/releases/tag/recordings-720p)
    if [ "$SCENARIO" = "dev" ]; then
        mkdir -p "$VIDEO_DIR"
        if command -v gh >/dev/null 2>&1; then
            for pattern in 'pexels-tom-fisk*' 'pexels-kelly*' 'pexels-ye-qiao*'; do
                # find membaca pattern literal — glob dalam 'ls "$dir/$pattern"'
                # tidak pernah match dan memicu gh ulang tiap kali (gagal menimpa
                # file yang sudah ada). Cek file nyata, bukan nama ber-glob.
                if ! find "$VIDEO_DIR" -maxdepth 1 -name "$pattern" -print -quit \
                        | grep -q .; then
                    gh release download recordings-720p --repo akhnafal-aban/HARNES-Server -D "$VIDEO_DIR" -p "$pattern"
                fi
            done
        fi
    fi

    echo "== Seed skenario '$SCENARIO' (diminta eksplisit) =="
    .venv/bin/python Tools/seed_demo.py --scenario "$SCENARIO" --video-dir "$VIDEO_DIR"
fi

if [ "$HOST" = "0.0.0.0" ] || [ "$HOST" = "127.0.0.1" ]; then
    if nc -z localhost "$PORT" 2>/dev/null; then
        cat >&2 <<EOF
PORT SIBUK: sudah ada proses yang mendengarkan port $PORT.
Kalau itu server ini yang masih hidup di terminal lain: buka terminal itu, tekan Ctrl+C.
Atau matikan instans lama:  pkill -f Sources.main
Atau pakai port lain:       HARNES_SERVER_PORT=$((PORT + 1)) sh $0
EOF
        exit 1
    fi
fi

if [ "$HOST" = "0.0.0.0" ]; then
    MAC_NAME="$(scutil --get LocalHostName 2>/dev/null || echo '<nama-mac>')"
    echo "== Server HARNES menyala =="
    echo "   Di Mac ini / simulator : http://localhost:$PORT/"
    echo "   iPhone fisik di LAN    : http://$MAC_NAME.local:$PORT/"
else
    echo "== Server HARNES menyala: http://$HOST:$PORT/ =="
fi
if [ "$AUTOCAM" = "1" ]; then
    echo "   Kamera: auto-start SEMUA (HARNES_AUTO_START_CAMERAS aktif)."
else
    echo "   Kamera: supervisor aktif — kamera desired_on menyala otomatis (opt-out: HARNES_CAMERA_SUPERVISOR=0)."
fi

# Watchdog: crash runtime GPU (mis. assertion Metal/MPS sporadis) tidak boleh
# mematikan monitoring site. Default AKTIF — matikan dengan HARNES_SERVER_WATCH=0.
# Restart cepat: thread playback dibuat segar.
#
# Kamera TIDAK otomatis dinyalakan di sini — murni start server. Kamera
# dinyalakan per kamera dari app HSE (POST /cameras/{id}/start). Opt-in untuk
# nyala-otomatis-semua: HARNES_AUTO_START_CAMERAS=1.
if [ "$AUTOCAM" = "1" ]; then
    set -- --host "$HOST" --port "$PORT" --auto-start-cameras
else
    set -- --host "$HOST" --port "$PORT"
fi

WATCH="${HARNES_SERVER_WATCH:-1}"
if [ "$WATCH" = "1" ]; then
    attempt=0
    while :; do
        attempt=$((attempt + 1))
        # Rantai && / || mengamankan exit-status python dari `set -eu` —
        # tanpa ini sh MATI saat crash GPU (fix 14 Nov: watchdog tak pernah
        # sampai baris restart-nya; MSG '[watch]' terbukti tak pernah dicetak).
        .venv/bin/python -m Sources.main "$@" && code=0 || code=$?
        echo "[watch] server keluar (code=$code, run #$attempt) — restart dalam 3 dtk… Ctrl+C untuk berhenti." >&2
        sleep 3
    done
fi
exec .venv/bin/python -m Sources.main "$@"
