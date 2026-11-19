#!/bin/bash
# Produser RTSP dari FILE VIDEO (loop) — kamera simulasi "CCTV live" dari
# rekaman, tanpa webcam/Continuity: video -> ffmpeg loop PUSH -> MediaMTX ->
# stream rtsp://127.0.0.1:8554/<nama-singkat>. Pendaftaran kamera di app iOS
# (HSE input URL RTSP) — script ini cukup CETAK LINK-nya.
# Sejajar start-mac-cam-rtsp.sh (webcam/iPhone -> /mac); broker MediaMTX sama.
#
# Pemakaian:
#   sh Scripts/start-video-rtsp.sh site-depo                  # auto-file: pexels-ye-qiao* di Server/recordings/
#   sh Scripts/start-video-rtsp.sh site-depo /path/video.mp4  # file eksplisit (nama bebas)
#   sh Scripts/start-video-rtsp.sh --list                     # stream yang sedang jalan
#   sh Scripts/start-video-rtsp.sh --stop site-depo           # matikan satu
#   sh Scripts/start-video-rtsp.sh --stop-all                 # matikan semua
#
# Nama singkat = path stream di MediaMTX: huruf kecil/angka/'-' saja.
# "mac" dipesan untuk produser webcam (start-mac-cam-rtsp.sh).
# Auto-file bawaan (sampel rilis recordings-720p) + padanan seed dev:
#   site-timur -> pexels-tom-fisk* ("Site Timur")
#   site-depo  -> pexels-ye-qiao*  ("Site Depo")
# Varian *720p* dipilih bila ada (encode lebih ringan; delivery server memang
# dibatasi 1280px). Berbeda dari kamera dev lama (path file di cameras.source):
# aliran INI adalah RTSP live — server membaca lewat RTSPReader seperti CCTV IP.
#
# Dependensi: brew install ffmpeg mediamtx   (sekali per Mac)
set -euo pipefail

PORT="8554"
PID_PREFIX="/tmp/harnes-video-rtsp"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REC_DIR="$SCRIPT_DIR/../recordings"

# Mode internal (dipanggil rekursif oleh start; bukan command user):
# jalankan ffmpeg SEKALI, catat alasan keluar, bersihkan PID — lalu berhenti.
# Sumber hilang/crash = berhenti; TIDAK ada restart otomatis (owner 13 Nov,
# konsisten dengan produser kamera: disconnect = perintah stop disengaja).
if [ "${1:-}" = "__watch" ]; then
    WATCH_NAME="${2:-}"
    WATCH_FILE="${3:-}"
    PID_CLEANUP="$PID_PREFIX-$WATCH_NAME.pid"
    FFPID=''
    trap 'kill "${FFPID:-}" 2>/dev/null || true; rm -f "$PID_CLEANUP" 2>/dev/null; exit 0' INT TERM
    if [ ! -f "$WATCH_FILE" ]; then
        echo "[produser] file tidak ada: $WATCH_FILE — produser berhenti, TIDAK auto-restart. Jalankan lagi bila perlu: $0 $WATCH_NAME <file>" >&2
        rm -f "$PID_CLEANUP"
        exit 1
    fi
    echo "[produser] $WATCH_FILE → rtsp://127.0.0.1:$PORT/$WATCH_NAME" >&2
    ffmpeg -hide_banner -loglevel warning \
        -stream_loop -1 -re -i "$WATCH_FILE" \
        -an -c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p -g 30 \
        -f rtsp "rtsp://127.0.0.1:$PORT/$WATCH_NAME" &
    FFPID=$!
    code=0
    wait "$FFPID" || code=$?   # guard set -e: keluar ≠ error
    echo "[produser] ffmpeg keluar (code $code) — produser TIDAK menyala ulang otomatis. Jalankan lagi bila perlu." >&2
    rm -f "$PID_CLEANUP"
    exit 0
fi

fail() { echo "$1" >&2; exit 1; }

alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

case "${1:-}" in
    --list)
        found=0
        for pid_file in "$PID_PREFIX"-*.pid; do
            [ -f "$pid_file" ] || continue
            name="${pid_file##*/}"; name="${name%.pid}"; name="${name#harnes-video-rtsp-}"
            if alive "$pid_file"; then
                echo "[jalan] rtsp://127.0.0.1:$PORT/$name  (pid $(cat "$pid_file"))"
                found=1
            else
                rm -f "$pid_file"
            fi
        done
        [ "$found" = 0 ] && echo "Tidak ada stream video berjalan."
        exit 0
        ;;
    --stop)
        NAME="${2:-}"
        [ -n "$NAME" ] || fail "Pakai: $0 --stop <nama-singkat>"
        pid_file="$PID_PREFIX-$NAME.pid"
        if alive "$pid_file"; then
            PRODUCER="$(cat "$pid_file")"
            # Sejajar cam: TERM → tunggu; 4 dtk anak -9; 6 dtk watcher -9.
            kill "$PRODUCER" 2>/dev/null || true
            n=0
            while kill -0 "$PRODUCER" 2>/dev/null && [ "$n" -lt 12 ]; do
                sleep 1
                n=$((n + 1))
                if [ "$n" -eq 4 ]; then
                    pkill -9 -f "rtsp://127.0.0.1:$PORT/$NAME\$" 2>/dev/null || true
                elif [ "$n" -eq 6 ]; then
                    kill -9 "$PRODUCER" 2>/dev/null || true
                fi
            done
            rm -f "$pid_file"
            echo "Stream '$NAME' dihentikan. (MediaMTX dibiarkan jalan — broker bersama.)"
        else
            rm -f "$pid_file"   # pid basi → bersihkan
            fail "Stream '$NAME' tidak jalan."
        fi
        exit 0
        ;;
    --stop-all)
        stopped=0
        for pid_file in "$PID_PREFIX"-*.pid; do
            [ -f "$pid_file" ] || continue
            pid="$(cat "$pid_file")"
            kill "$pid" 2>/dev/null || true
            rm -f "$pid_file"
            stopped=$((stopped + 1))
        done
        echo "$stopped stream dihentikan. (MediaMTX dibiarkan jalan — broker bersama.)"
        exit 0
        ;;
esac

NAME="${1:-}"
FILE="${2:-}"

# Nama singkat jadi path stream — validasi ketat supaya URL selalu sehat.
case "$NAME" in
    ''|*[!a-z0-9-]*) fail "Nama singkat wajib huruf kecil/angka/'-' (contoh: site-depo), dapat: '$NAME'." ;;
esac
[ "${#NAME}" -le 64 ] || fail "Nama terlalu panjang (max 64 karakter)."
[ "$NAME" != "mac" ] || fail "Nama 'mac' dipesan untuk produser webcam (start-mac-cam-rtsp.sh)."

# Auto-file untuk nama sampel bawaan; nama lain = wajib file eksplisit.
if [ -z "$FILE" ]; then
    case "$NAME" in
        site-timur) pat='pexels-tom-fisk*' ;;
        site-depo)  pat='pexels-ye-qiao*' ;;
        *) fail "Nama '$NAME' belum ada auto-file-nya — beri file video eksplisit: $0 $NAME /path/video.mp4 (atau pakai nama sampel: site-timur|site-depo)." ;;
    esac
    # Varian 720p lebih ringan; ambil itu bila ada, selain itu file pertama.
    # (ls "$REC_DIR"/$pat sudah menghasilkan path absolut/penuh — jangan
    # di-prefix folder lagi.)
    FILE="$(ls "$REC_DIR"/$pat 2>/dev/null | grep 720p | head -1 || true)"
    [ -n "$FILE" ] || FILE="$(ls "$REC_DIR"/$pat 2>/dev/null | head -1 || true)"
    [ -n "$FILE" ] || fail "Video tidak ketemu di $REC_DIR (pola $pat). Unduh sampel: gh release download recordings-720p --repo akhnafal-aban/HARNES-Server -D $REC_DIR — atau beri path file eksplisit."
fi
[ -f "$FILE" ] || fail "File video tidak ditemukan: $FILE"

command -v ffmpeg >/dev/null 2>&1 || fail "ffmpeg belum terpasang — jalankan: brew install ffmpeg"

# Broker: MediaMTX harus hidup di port 8554 (sekali untuk semua kamera tim).
if ! nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
    command -v mediamtx >/dev/null 2>&1 || fail "MediaMTX belum terpasang — jalankan: brew install mediamtx"
    echo "Menyalakan MediaMTX (broker RTSP)…"
    CONF="$SCRIPT_DIR/../mediamtx.yml"
    [ -f "$CONF" ] || CONF=/opt/homebrew/etc/mediamtx/mediamtx.yml
    nohup mediamtx "$CONF" > /tmp/mediamtx.log 2>&1 &
    sleep 2
fi

pid_file="$PID_PREFIX-$NAME.pid"
if alive "$pid_file"; then
    fail "Stream '$NAME' sudah jalan (pid $(cat "$pid_file")) — matikan dulu: $0 --stop $NAME"
fi

# -stream_loop -1 = loop abadi; -re = pace realtime (VOD jadi live); -an = buang
# audio (pipeline server video-only). x264 zerolatency supaya delay kecil.
# Wrapper sekali-pakai: sumber file hilang/ffmpeg mati → berhenti + PID dibersih-
# kan; TIDAK auto-restart (owner 13 Nov — konsisten produser kamera).
nohup sh "$0" __watch "$NAME" "$FILE" \
    > "/tmp/harnes-video-rtsp-$NAME.log" 2>&1 &

echo $! > "$pid_file"
sleep 2
if alive "$pid_file"; then
    MAC_NAME="$(scutil --get LocalHostName 2>/dev/null || echo '<nama-mac>')"
    echo "OK — stream '$NAME' hidup (pid $(cat "$pid_file")), loop otomatis: $FILE"
    echo "   Bila file hilang/ffmpeg mati: stream berhenti — TIDAK nyala ulang otomatis."
    echo ""
    echo "== Link RTSP — salin ke app iOS (input kamera RTSP) =="
    echo "   Server di Mac ini  : rtsp://127.0.0.1:$PORT/$NAME"
    echo "   Server di Mac lain : rtsp://$MAC_NAME.local:$PORT/$NAME"
    echo "Log: /tmp/harnes-video-rtsp-$NAME.log"
else
    echo "GAGAL nyala — cek /tmp/harnes-video-rtsp-$NAME.log" >&2
    rm -f "$pid_file"
    exit 1
fi
