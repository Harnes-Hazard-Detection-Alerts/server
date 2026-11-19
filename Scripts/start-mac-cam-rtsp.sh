#!/bin/bash
# Produser RTSP dari kamera Mac/iPhone — jembatan "CCTV live" untuk server HARNES.
#
# Kamera (iPhone Continuity / webcam Mac) -> ffmpeg PUSH -> MediaMTX (broker RTSP)
# -> server HARNES menarik rtsp://127.0.0.1:8554/mac. MediaMTX dipakai karena
# ffmpeg 8 tidak lagi mendukung mode listen pada output RTSP; broker juga jadi
# jalur standar industri (multi-klien, dipakai bersama tim, CCTV IP nanti).
#
# iPhone memakai kanal Continuity (WAJIB dekat Mac ini, layar terkunci, mount,
# landscape, Apple ID sama). Webcam Mac biasa = device 0, tanpa syarat itu.
# CCTV IP komersial punya jalurnya sendiri: setup-cctv-rtsp.sh (probe path →
# daftar → verifikasi frame mengalir). Intake tetap LANGSUNG: server HARNES
# menarik rtsp://user:pass@kamera:554/path tanpa relay.
#
# Pemakaian (jalankan dari Terminal.app — izin kamera macOS muncul sekali):
#   sh Scripts/start-mac-cam-rtsp.sh              # auto: iPhone Continuity pertama
#   sh Scripts/start-mac-cam-rtsp.sh --{nama-kamera}
#       # by NAMA device — parameter apa pun dari --list. Keyword case-insensitive,
#       # boleh beberapa kata dipisah '-'; SEMUA kata harus ada di nama device.
#       # Contoh: --mac (MacBook Pro Camera), --abann (Abann Camera),
#       #         --iphone-ada (iPhone Air ADA), --iphone (iPhone Continuity mana pun).
#   sh Scripts/start-mac-cam-rtsp.sh --choose    # menu anggota tim: pilih device dari daftar
#   sh Scripts/start-mac-cam-rtsp.sh 0           # pakai index manual (lihat --list)
#   sh Scripts/start-mac-cam-rtsp.sh --list      # daftar MENTAH semua device (debug)
#   sh Scripts/start-mac-cam-rtsp.sh --list-clean # daftar kamera saja (bersih, utk menu)
#   sh Scripts/start-mac-cam-rtsp.sh --status   # produser hidup? + petunjuk log
#   sh Scripts/start-mac-cam-rtsp.sh --probe 0   # still 1 frame → /tmp/harnes-cam-probe.jpg
#   sh Scripts/start-mac-cam-rtsp.sh --stop      # matikan produser
#
# Perilaku berhenti (owner 13 Nov 2026): disconnect kamera = perintah stop
# yang disengaja — produser berhenti, mencatat alasan keluar di log, membersih-
# kan PID (status selalu akurat), TIDAK menyala ulang sendiri. Nyalakan lagi
# manual bila diperlu.
#
# Dependensi: brew install mediamtx ffmpeg   (sekali saja)
# Lalu di server (repo root):
#   python -m Sources.cameras.cli add rtsp://127.0.0.1:8554/mac \
#       --name "Site Live" --location "Site Utara" --fps 5
#   # daftar lewat app HSE lebih utama (register = langsung streaming, 13 Nov);
#   # nyalakan lewat server: HARNES_AUTO_START_CAMERAS=1, atau POST /cameras/{id}/start
set -euo pipefail

DEVICE="${1:-auto}"
PORT="8554"
PID_FILE="/tmp/harnes-mac-cam-rtsp.pid"

# resolve <nama-keyword> → index device kamera VIDEO pertama yang cocok,
# abaikan kamera virtual "Desk View". Index bergeser antar-reboot, maka
# nama — bukan angka — yang jadi kunci command di bawah. Keyword bebas
# (case-insensitive) dan boleh beberapa kata dipisah '-' — semua kata
# harus ada di nama device: "iphone-ada" → "iPhone Air ADA".
resolve() {
    ffmpeg -hide_banner -f avfoundation -list_devices true -i "" 2>&1 \
        | awk -v kw="$1" 'BEGIN { n = split(tolower(kw), toks, "-"); if (n == 0) exit }
                          /AVFoundation video devices/{v=1;next}
                          /AVFoundation audio devices/{v=0}
                          v && $0 !~ /Desk View/ && $0 !~ /Capture screen/ && match($0, /\[[0-9]+\] /) {
                              name = tolower($0); hit = 1
                              for (i = 1; i <= n; i++) if (!index(name, toks[i])) { hit = 0; break }
                              if (hit) { print substr($0, RSTART+1, RLENGTH-3); exit }
                          }' || true   # daftar device ffmpeg selalu exit != 0; jangan bunuh skrip (set -e)
}

# Mode internal (dipanggil rekursif oleh start; bukan command user):
# jalankan ffmpeg SEKALI, catat alasan keluar, bersihkan PID — lalu berhenti.
# Disconnect kamera = berhenti DISERANGKAI sengaja (owner 13 Nov 2026):
# TIDAK ada restart otomatis; state rapi supaya --status/harnes selalu benar.
if [ "${1:-}" = "__watch" ]; then
    WATCH_KEY="${2:-}"
    FFPID=''
    trap 'kill "${FFPID:-}" 2>/dev/null || true; rm -f "$PID_FILE" 2>/dev/null; exit 0' INT TERM
        # Kunci = NAMA device (bukan index — bergeser antar-reboot/USB).
        case "$WATCH_KEY" in
            ''|*[!0-9]*) DEV="$(resolve "$WATCH_KEY")" ;;
            *) DEV="$WATCH_KEY" ;;
        esac
        if [ -z "$DEV" ]; then
            echo "[produser] kamera '$WATCH_KEY' tidak terlihat (terlepas?) — produser berhenti, TIDAK auto-restart. Nyalakan lagi bila perlu: $0 --{nama-kamera}" >&2
            rm -f "$PID_FILE"
            exit 1
        fi
        echo "[produser] device $DEV → rtsp://127.0.0.1:$PORT/mac" >&2
        ffmpeg -hide_banner -loglevel warning \
            -f avfoundation -pixel_format yuyv422 -framerate 30 -i "$DEV" \
            -an -vf fps=12 -c:v libx264 -preset veryfast -tune zerolatency \
            -pix_fmt yuv420p -g 30 \
            -f rtsp "rtsp://127.0.0.1:$PORT/mac" &
        FFPID=$!
        code=0
        wait "$FFPID" || code=$?   # guard set -e: keluar ≠ error
        echo "[produser] ffmpeg keluar (code $code) — disconnect/berhenti memang disengaja: produser TIDAK menyala ulang otomatis (owner 13 Nov). Jalankan lagi bila perlu." >&2
        rm -f "$PID_FILE"
        exit 0
fi


case "$DEVICE" in
    --stop|--list|--list-clean|--probe|--choose|--status) ;;   # flag khusus — ditangani blok di bawah
    --*)
        NAME="${DEVICE#--}"
        DEVICE=$(resolve "$NAME")
        if [ -z "$DEVICE" ]; then
            echo "Kamera '$NAME' tidak ketemu — cek daftar device: $0 --list" >&2
            exit 1
        fi
        WATCH_KEY="$NAME"      # watcher mengulang BY NAMA (anti-pergeseran index)
        echo "Kamera '$NAME' → device $DEVICE"
        ;;
esac

if [ "$DEVICE" = "--stop" ]; then
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        PRODUCER="$(cat "$PID_FILE")"
        # TERM dulu; trap watcher menunggu sleep-foreground selesai — maka
        # --stop sendiri menunggu & menjenjangi: 4 dtk bunuh anak ffmpeg -9,
        # 6 dtk bunuh watcher -9. Janji: setelah perintah ini BENAR-BENAR mati.
        kill "$PRODUCER" 2>/dev/null || true
        n=0
        while kill -0 "$PRODUCER" 2>/dev/null && [ "$n" -lt 12 ]; do
            sleep 1
            n=$((n + 1))
            if [ "$n" -eq 4 ]; then
                pkill -9 -f "rtsp://127.0.0.1:$PORT/mac\$" 2>/dev/null || true
            elif [ "$n" -eq 6 ]; then
                kill -9 "$PRODUCER" 2>/dev/null || true
            fi
        done
        rm -f "$PID_FILE"
        echo "Produser RTSP dihentikan. (MediaMTX dibiarkan jalan — broker kamera bersama.)"
    else
        rm -f "$PID_FILE"   # pid basi → bersihkan
        echo "Produser tidak jalan."
    fi
    exit 0
fi

if [ "$DEVICE" = "--status" ]; then
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "Produser HIDUP (pid $(cat "$PID_FILE")) — rtsp://127.0.0.1:$PORT/mac"
        echo "Log: /tmp/harnes-mac-cam-rtsp.log (baris [watch] = siklus auto-restart)"
    else
        rm -f "$PID_FILE"
        echo "Produser mati." >&2
        exit 1
    fi
    exit 0
fi

if [ "$DEVICE" = "--probe" ]; then
    TARGET="${2:-auto}"
    if [ "$TARGET" = "auto" ]; then
        TARGET=$(ffmpeg -hide_banner -f avfoundation -list_devices true -i "" 2>&1 \
            | awk '/AVFoundation video devices/{v=1;next}
                    /AVFoundation audio devices/{v=0}
                    v && /iPhone/ { if (match($0, /\[[0-9]+\] /)) { print substr($0, RSTART+1, RLENGTH-3); exit } }' \
            || true)
        [ -z "$TARGET" ] && TARGET=0
    fi
    OUT="/tmp/harnes-cam-probe.jpg"
    echo "Mengambil still 6 detik dari device $TARGET → $OUT …"
    ffmpeg -hide_banner -loglevel warning \
        -f avfoundation -pixel_format yuyv422 -framerate 30 -video_size 1280x720 \
        -i "$TARGET" -t 6 -frames:v 45 -vf "select=eq(n\\,42)" -frames:v 1 -q:v 3 -y "$OUT"
    if [ -s "$OUT" ]; then
        echo "OK — hasil: $OUT (buka: open $OUT)"
    else
        echo "GAGAL — device $TARGET tidak menghasilkan frame (izin kamera / device salah)."
        exit 1
    fi
    exit 0
fi

if [ "$DEVICE" = "--list" ]; then
    ffmpeg -hide_banner -f avfoundation -list_devices true -i "" 2>&1 | sed -n '/AVFoundation/,$p' || true
    exit 0
fi

# --list-clean: daftar kamera video ASLI saja (tanpa audio device, tanpa Desk
# View, tanpa capture layar, tanpa log error ffmpeg) — untuk tampilan menu.
if [ "$DEVICE" = "--list-clean" ]; then
    ffmpeg -hide_banner -f avfoundation -list_devices true -i "" 2>&1 \
        | awk '/AVFoundation video devices/{v=1;next}
                /AVFoundation audio devices/{v=0}
                v && $0 !~ /Desk View/ && $0 !~ /Capture screen/ && match($0, /\[[0-9]+\] /) {
                    print substr($0, RSTART) }' || true
    exit 0
fi

# --choose: menu pilih device untuk anggota tim yang belum tahu nama/idx
# kamera di Mac mereka. Tampilkan daftar kamera video asli (bukan Desk View,
# bukan capture layar), lalu baca pilihan nomor. Input kosong/non-angka ditolak.
if [ "$DEVICE" = "--choose" ]; then
    echo "== Kamera video di Mac ini =="
    ffmpeg -hide_banner -f avfoundation -list_devices true -i "" 2>&1 \
        | awk '/AVFoundation video devices/{v=1;next}
                /AVFoundation audio devices/{v=0}
                v && $0 !~ /Desk View/ && $0 !~ /Capture screen/ && match($0, /\[[0-9]+\] /) {
                    print substr($0, RSTART)
                }' || true
    printf 'Pilih nomor device kamera: '
    if ! read -r CHOICE; then
        echo "Input tidak terbaca — jalankan script ini dari Terminal.app (bukan dari agent/IDE/pipe)." >&2
        exit 1
    fi
    case "$CHOICE" in
        *[!0-9]*|'')
            echo "Pilihan tidak valid: '$CHOICE' — masukkan angka dari daftar di atas (atau cek ulang: $0 --list)." >&2
            exit 1
            ;;
    esac
    DEVICE="$CHOICE"
    # Watch key = NAMA device pilihan (kalau bisa) supaya auto-restart tahan
    # pergeseran index; angka dipakai apa adanya bila nama tak terbaca.
    WATCH_KEY=$(ffmpeg -hide_banner -f avfoundation -list_devices true -i "" 2>&1 \
        | awk -v want="$CHOICE" '/AVFoundation video devices/{v=1;next}
                /AVFoundation audio devices/{v=0}
                v && $0 !~ /Desk View/ && $0 !~ /Capture screen/ && match($0, /\[[0-9]+\] /) {
                    idx = substr($0, RSTART+1, RLENGTH-3)
                    if (idx == want) { print substr($0, RSTART+RLENGTH); exit } }' || true)
    [ -n "$WATCH_KEY" ] || WATCH_KEY="$CHOICE"
fi

# Default "auto": pilih device pertama yang namanya mengandung iPhone
# (Continuity Camera) — index bisa bergeser antar-reboot/koneksi display,
# jadi dicari BY NAME, bukan angka tetap. Match() menangkap "[N] " murni
# agar prefix log ffmpeg ("[AVFoundation indev @ 0x…]") tak ikut kecolong.
if [ "$DEVICE" = "auto" ]; then
    DEVICE=$(ffmpeg -hide_banner -f avfoundation -list_devices true -i "" 2>&1 \
        | awk '/AVFoundation video devices/{v=1;next}
                /AVFoundation audio devices/{v=0}
                v && /iPhone/ { if (match($0, /\[[0-9]+\] /)) { print substr($0, RSTART+1, RLENGTH-3); exit } }' \
        || true)
    if [ -z "$DEVICE" ]; then
        DEVICE=0
        WATCH_KEY='iphone'
        echo "Kamera iPhone tidak terdeteksi — fallback webcam MacBook (device 0)."
        echo "Pastikan iPhone terkunci+mount+dekat, Apple ID sama, Wi-Fi & Bluetooth ON."
    else
        WATCH_KEY='iphone'
        echo "Auto-detect: kamera iPhone = device $DEVICE"
    fi
fi

# Guard: index device wajib angka murni. Menangkap salah kirim argumen —
# mis. teks "#..." yang di zsh interaktif (INTERACTIVE_COMMENTS default OFF)
# TIDAK jadi komentar, melainkan terus sebagai argumen $1.
case "$DEVICE" in
    ''|*[!0-9]*)
        echo "Device index tidak valid: '$DEVICE' — beri angka (mis. 0 atau 1), atau kosongkan untuk auto-detect. Jangan tambah komentar '#' di akhir baris." >&2
        exit 1
        ;;
    *)  [ -n "${WATCH_KEY:-}" ] || WATCH_KEY="$DEVICE"   # index manual: watch pakai index apa adanya
        ;;
esac

# Broker: MediaMTX harus hidup di port 8554 (sekali untuk semua kamera tim).
if ! nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
    if ! command -v mediamtx >/dev/null; then
        echo "MediaMTX belum terpasang — jalankan: brew install mediamtx" >&2
        exit 1
    fi
    echo "Menyalakan MediaMTX (broker RTSP)…"
    CONF="$SCRIPT_DIR/../mediamtx.yml"
    [ -f "$CONF" ] || CONF=/opt/homebrew/etc/mediamtx/mediamtx.yml
    nohup mediamtx "$CONF" > /tmp/mediamtx.log 2>&1 &
    sleep 2
fi

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Produser sudah jalan (pid $(cat "$PID_FILE")) — matikan dulu: $0 --stop" >&2
    exit 1
fi

echo "== Produser RTSP kamera Mac =="
echo "   device : $DEVICE  (daftar semua: $0 --list)"
echo "   catatan: dialog izin kamera macOS muncul sekali — klik Allow/OK."

# Produser jalan dalam wrapper sekali-pakai (owner 13 Nov): disconnect kamera
# = perintah stop yang disengaja — ffmpeg keluar → wrapper mencatat alasan di
# log + menghapus PID supaya --status/harnes akurat, lalu berhenti. TIDAK ada
# restart otomatis. Sisi server punya penanganannya sendiri (RTSPReader
# reconnect 3x → error/fallback → HSE start ulang dari app).
nohup sh "$0" __watch "$WATCH_KEY" > /tmp/harnes-mac-cam-rtsp.log 2>&1 &

echo $! > "$PID_FILE"
sleep 2
if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    MAC_NAME="$(scutil --get LocalHostName 2>/dev/null || echo '<nama-mac>')"
    echo "OK — produser hidup (pid $(cat "$PID_FILE"))."
    echo "   Bila kamera di-disconnect: produser berhenti sendiri (tidak nyala ulang otomatis)."
    echo "   Cek kapan pun: $0 --status   ·   Matikan: $0 --stop"
    echo ""
    echo "== Link RTSP — salin ke app iOS (input kamera RTSP) =="
    echo "   Server di Mac ini  : rtsp://127.0.0.1:$PORT/mac"
    echo "   Server di Mac lain : rtsp://$MAC_NAME.local:$PORT/mac"
    echo "Log: /tmp/harnes-mac-cam-rtsp.log"
else
    echo "GAGAL nyala — cek /tmp/harnes-mac-cam-rtsp.log (umumnya izin kamera / device index salah / resolusi)" >&2
    rm -f "$PID_FILE"
    exit 1
fi
