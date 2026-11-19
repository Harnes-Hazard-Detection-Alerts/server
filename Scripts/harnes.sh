#!/bin/sh
# HARNES — satu pintu untuk semua script tim: menu interaktif
# + perintah langsung untuk agent/CI. MURNI ORKESTRATOR — kebijakan tetap
# di script yang dipanggil (start-site-server, start-*-rtsp, *-database).
#
# Menu      : sh Scripts/harnes.sh            (butuh Terminal.app)
# Langsung  : server | server-stop | kamera … | kamera-list | cctv … | video <nama> [file]
#            video-list | video-stop <nama> | video-stop-all | ops-relogin
#            db-setup | db-migrate | db-reset dev|prod | db-backup <path> | status
#
# Opsi 3 (RTSP URL Source) → 1 = CCTV IP asli: probe path RTSP → daftar ke DB
# → verifikasi frame mengalir (setup-cctv-rtsp.sh). Opsi 3/4 (produser RTSP)
# otomatis mendaftarkan kamera ke database bila server jalan (cara yang sama
# seperti input app HSE) — langsung terlihat di iOS. Ctrl+C = sinyal terminal
# biasa: hentikan server/aksi dan keluar.
set -u

# Handler INT eksplisit: tanpa trap, shell menunggu proses anak dan tetap
# hidup setelah server berhenti — satu ^C harus menghentikan semuanya.
trap 'exit 130' INT

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${HARNES_SERVER_PORT:-8080}"          # HTTP server site
RTSP_PORT="${HARNES_RTSP_PORT:-8554}"        # broker MediaMTX
MAC_NAME="$(scutil --get LocalHostName 2>/dev/null || echo '<nama-mac>')"

# util
confirm() {   # confirm <pertanyaan> → 0 = ya
    printf '%s [y/N]: ' "$1"
    read -r JAWAB || return 1
    case "$JAWAB" in y|Y|ya|YA|yes|YES) return 0 ;; *) return 1 ;; esac
  }

alive_pid_file() {   # $1 = path pid file → cetak pid bila prosesnya hidup
    [ -f "$1" ] || return 1
    pid="$(cat "$1")"
    kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}

server_pid() {
    for pid in $(pgrep -f 'Sources.main' 2>/dev/null); do
        echo "$pid"
        return 0
    done
    return 1
}

run() {   # JANGAN pasang `trap '' INT` di sini: disposition "ignore"
          # terwarisi sh/python anak → Ctrl+C tak berefek sama sekali.
    "$@"
    code=$?
    [ "$code" = 0 ] || printf '[harnes] perintah selesai dengan status %s\n' "$code"
    return 0
}

rtsp_url() { printf 'rtsp://127.0.0.1:%s/%s' "$RTSP_PORT" "$1"; }

# registrasi kamera ke database (persis input app HSE)
# POST /cameras → auto-start: kamera langsung streaming begitu terdaftar.
# Setelah itu start EKSPLISIT (POST start) + desired_on=true supaya kamera
# tampil & tetap hidup di app, bukan sekadar terdaftar.
# Akun HSE: default demo; override HARNES_HSE_USER / HARNES_HSE_PASS.
register_rtsp() {   # $1 = nama tampilan, $2 = lokasi, $3 = rtsp://…
    PYBIN="$SCRIPT_DIR/../.venv/bin/python"
    CAM_ID=''      # diisi dari respons POST /cameras (201) — mulai kosong (fix 19 Nov: set -u bikin "unbound variable")
    if ! nc -z localhost "$PORT" 2>/dev/null; then
        echo '! Server tidak jalan — kamera BELUM masuk database.'
        echo '  Nyalakan server (opsi 1), atau daftarkan manual di app HSE: link tercetak di atas.'
        return 1
    fi
    if [ ! -x "$PYBIN" ]; then
        echo '! .venv belum ada (opsi 6: setup) — daftarkan manual di app HSE.'
        return 1
    fi
    TOKEN=$(printf '{"username":"%s","password":"%s"}' \
            "${HARNES_HSE_USER:-anything}" "${HARNES_HSE_PASS:-anythingcuy06}" \
        | curl -s --max-time 5 -X POST "http://localhost:$PORT/auth/login" \
            -H 'Content-Type: application/json' --data-binary @- \
        | "$PYBIN" -c 'import json,sys
try:
    print(json.load(sys.stdin)["access_token"])
except Exception:
    sys.exit(1)') || TOKEN=''
    if [ -z "$TOKEN" ]; then
        echo '! Login HSE gagal (user: '"${HARNES_HSE_USER:-anything}"') — daftarkan manual di app HSE.'
        return 1
    fi
    REG="/tmp/harnes-reg.$$"
    HTTP=$(printf '{"name":"%s","location":"%s","type":"rtsp","source":"%s"}' "$1" "$2" "$3" \
        | curl -s --max-time 8 -o "$REG" -w '%{http_code}' -X POST \
            "http://localhost:$PORT/cameras" \
            -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' --data-binary @-)
    CAM_ID=$(printf '%s' "${CAM_ID:-}" | tr -d '[:space:]')
    case "$HTTP" in
        201) CAM_ID=$("$PYBIN" -c 'import json,sys
try:
    print(json.load(open(sys.argv[1]))["id"])
except Exception:
    print("")' "$REG")
           echo "OK — kamera '$1' MASUK DATABASE + langsung streaming. Buka app iOS (HSE)."
           ;;
        409) echo "Sudah terdaftar (sumber sama) — pakai kamera yang ada di app."
             CAM_ID=$(curl -s --max-time 8 "http://localhost:$PORT/cameras" \
                 -H "Authorization: Bearer $TOKEN" \
                 | "$PYBIN" -c 'import json,sys
try:
    items = json.load(sys.stdin)["items"]
    print(next((c["id"] for c in items if c["source"] == sys.argv[1]), ""))
except Exception:
    print("")' "$3")
             ;;
        *)   echo "! Daftar gagal (HTTP $HTTP): $(head -c 160 "$REG")"
             echo "  Daftarkan manual di app HSE: $3" ;;
    esac
    # Start eksplisit + desired_on=true → kamera tampil hidup di app.
    if [ -n "$CAM_ID" ]; then
        curl -s --max-time 8 -o /dev/null -X POST "http://localhost:$PORT/cameras/$CAM_ID/start" \
            -H "Authorization: Bearer $TOKEN" >/dev/null 2>&1 || true
        echo "   start + desired_on → kamera aktif & tampil di app."
    fi
    rm -f "$REG"
    return 0
}

# buang kamera dari database + stop produsernya (simulasi).
# $1 = nama stream (path RTSP) mis. site-barat / mac
# Mencari kamera dengan source rtsp://127.0.0.1:<port>/<nama> lalu DELETE.
unregister_rtsp() {   # $1 = nama stream (tanpa rtsp://)
    PYBIN="$SCRIPT_DIR/../.venv/bin/python"
    if ! nc -z localhost "$PORT" 2>/dev/null; then
        echo '! Server tidak jalan — tak bisa buang dari DB.'
        return 1
    fi
    if [ ! -x "$PYBIN" ]; then
        echo '! .venv belum ada (opsi 6: setup).'
        return 1
    fi
    TOKEN=$(printf '{"username":"%s","password":"%s"}' \
            "${HARNES_HSE_USER:-anything}" "${HARNES_HSE_PASS:-anythingcuy06}" \
        | curl -s --max-time 5 -X POST "http://localhost:$PORT/auth/login" \
            -H 'Content-Type: application/json' --data-binary @- \
        | "$PYBIN" -c 'import json,sys
try:
    print(json.load(sys.stdin)["access_token"])
except Exception:
    sys.exit(1)') || TOKEN=''
    if [ -z "$TOKEN" ]; then
        echo '! Login HSE gagal.'
        return 1
    fi
    SRC="$(rtsp_url "$1")"
    CAM_ID=$(curl -s --max-time 8 "http://localhost:$PORT/cameras" \
        -H "Authorization: Bearer $TOKEN" \
        | "$PYBIN" -c 'import json,sys
src = sys.argv[1]
try:
    for cam in json.load(sys.stdin)["items"]:
        if cam.get("source") == src:
            print(cam["id"]); break
except Exception:
    pass' "$SRC")
    if [ -z "$CAM_ID" ]; then
        echo "   kamera '$1' tidak terdaftar di DB — cukup stop produsernya."
    else
        curl -s --max-time 8 -o /dev/null -X DELETE "http://localhost:$PORT/cameras/$CAM_ID" \
            -H "Authorization: Bearer $TOKEN" >/dev/null 2>&1 || true
        echo "   DB: kamera '$1' di-DROP (source $SRC)."
    fi
    # stop produser terkait (video + Mac/iPhone)
    sh "$SCRIPT_DIR/start-video-rtsp.sh" --stop "$1" >/dev/null 2>&1 || true
    if [ "$1" = "mac" ]; then
        sh "$SCRIPT_DIR/start-mac-cam-rtsp.sh" --stop >/dev/null 2>&1 || true
    fi
    echo "   produser '$1' dihentikan."
    return 0
}

# preset video sampel (satu tabel: pola file → short|nama|lokasi)
preset_of() {   # $1 = path file → 'short|nama tampilan|lokasi'
    case "$1" in
        *pexels-tom-fisk*) echo 'site-timur|Site Timur|Area Galian Timur' ;;
        *pexels-ye-qiao*)  echo 'site-depo|Site Depo|Halaman Depo' ;;
        *)
            stem="${1##*/}"; stem="${stem%.*}"
            short=$(printf '%s' "$stem" | tr '[:upper:]' '[:lower:]' \
                    | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')
            echo "$short|$stem|Rekaman video"
            ;;
    esac
}

# panel status
show_status() {
    echo '================================================================'
    if pid="$(server_pid)"; then
        echo " Server   : HIDUP (pid $pid) — http://localhost:$PORT"
        echo "            iPhone: http://$MAC_NAME.local:$PORT"
    else
        echo ' Server   : mati'
    fi
    if nc -z 127.0.0.1 "$RTSP_PORT" 2>/dev/null; then
        echo " MediaMTX : jalan (broker RTSP :$RTSP_PORT)"
    else
        echo ' MediaMTX : mati (produser kamera butuh broker ini)'
    fi
    if pid="$(alive_pid_file /tmp/harnes-mac-cam-rtsp.pid)"; then
        echo " Produser : kamera Mac/iPhone HIDUP (pid $pid) → $(rtsp_url mac)"
    else
        echo ' Produser : kamera Mac/iPhone mati'
    fi
    found=''
    for f in /tmp/harnes-video-rtsp-*.pid; do
        [ -f "$f" ] || continue
        pid="$(alive_pid_file "$f")" || { rm -f "$f"; continue; }
        name="${f##*/}"; name="${name%.pid}"; name="${name#harnes-video-rtsp-}"
        echo " Produser : video '$name' HIDUP (pid $pid) → $(rtsp_url "$name")"
        found=1
    done
    [ -n "$found" ] || echo ' Produser : video RTSP tidak ada yang jalan'
    local health api_ready=0
    health="$(curl -fsS --max-time 2 "http://localhost:$PORT/health" 2>/dev/null || true)"
    if [ -n "$health" ]; then
        echo "$health"
        api_ready=1
    else
        echo ' (health server tidak terjangkau)'
    fi
    # Kamera CCTV IP (password disamarkan oleh skripnya)
    if [ -x "$SCRIPT_DIR/../.venv/bin/python" ]; then
        if [ "$api_ready" -eq 1 ]; then
            cctv_rows="$(sh "$SCRIPT_DIR/setup-cctv-rtsp.sh" --list 2>/dev/null \
                | grep 'rtsp://' | grep -v '127\.0\.0\.1' || true)"
            if [ -n "$cctv_rows" ]; then
                printf '%s\n' "$cctv_rows" | while IFS= read -r row; do
                    echo " CCTV    : $row"
                done
            fi
        else
            echo ' CCTV    : daftar dilewati (API belum siap)'
        fi
    fi
    echo '================================================================'
}

# aksi server
start_server() {
    if pid="$(server_pid)"; then
        echo "Server SUDAH jalan (pid $pid)."
        echo ' a) biarkan — lihat /health'
        echo ' b) restart (stop lalu nyalakan ulang)'
        echo ' c) stop saja'
        echo ' d) batal'
        printf 'Pilih: '
        read -r SUB || return 0
        case "$SUB" in
            a) run curl -s "http://localhost:$PORT/health"; echo ;;
            b) if confirm "Hentikan server pid $pid lalu start ulang?"; then
                    kill "$pid" 2>/dev/null || true
                    sleep 1
                    run sh "$SCRIPT_DIR/start-site-server.sh"
                fi ;;
            c) stop_server ;;
            *) return 0 ;;
        esac
    else
        run sh "$SCRIPT_DIR/start-site-server.sh"
    fi
}

stop_server() {
    if ! pid="$(server_pid)"; then
        echo 'Server tidak jalan.'
        return 0
    fi
    if confirm "Hentikan server HARNES (pid $pid)?"; then
        # Matikan watchdog dulu — cegah respawn 3 detik — baru python.
        # Bug lapangan 17 Nov: hanya pkill Sources.main → start-site-server.sh
        # (loop watch) hidup dan menyalakan server baru dengan pid lain.
        pkill -f 'start-site-server' 2>/dev/null || true
        pkill -f 'Sources.main' 2>/dev/null || true
        n=0
        while server_pid >/dev/null 2>&1 && [ "$n" -lt 10 ]; do
            sleep 1
            n=$((n + 1))
            [ "$n" -eq 5 ] && pkill -9 -f 'Sources.main' 2>/dev/null || true
        done
        server_pid >/dev/null 2>&1 \
            && echo 'Masih hidup — coba lagi.' \
            || echo 'Server dihentikan (watchdog + server mati).'
    else
        echo 'Dibatalkan.'
    fi
}

# aksi kamera Mac/iPhone
start_mac_cam() {
    echo 'Cara pilih kamera:'
    echo ' 1) menu daftar device (--choose, paling aman)'
    echo ' 2) by nama  (mis. iphone-ada → tulis tanpa awalan --)'
    echo ' 3) otomatis (iPhone Continuity pertama / fallback webcam)'
    echo ' 4) daftar device (--list-clean)'
    echo ' 5) uji 1 frame still (--probe)'
    echo ' 6) matikan produser (--stop)'
    echo ' 7) BUANG dari DB + stop (simulasi: hapus baris kamera di server)'
    echo ' 0) batal'
    printf 'Pilih: '
    read -r SUB || return 0
    CAM="$SCRIPT_DIR/start-mac-cam-rtsp.sh"
    case "$SUB" in
        1) sh "$CAM" --choose \
             && register_rtsp 'Site Live' 'Kamera Mac/iPhone' "$(rtsp_url mac)" ;;
        2) printf 'Nama kamera (tanpa --): '
           read -r NAMA || return 0
           [ -n "$NAMA" ] && sh "$CAM" "--$NAMA" \
             && register_rtsp "Site Live ($NAMA)" 'Kamera Mac/iPhone' "$(rtsp_url mac)" ;;
        3) sh "$CAM" \
             && register_rtsp 'Site Live (iPhone)' 'Continuity iPhone' "$(rtsp_url mac)" ;;
        4) run sh "$CAM" --list-clean ;;
        5) printf 'Nomor device (enter = 0): '
           read -r DEV || return 0
           run sh "$CAM" --probe "${DEV:-0}" ;;
        6) run sh "$CAM" --stop ;;
        7) unregister_rtsp mac ;;
        *) return 0 ;;
    esac
}

# aksi video rekaman
choose_video_file() {   # $1 = folder → FILE (kosong = batal) — daftar bernomor
    DIR="$1"
    LIST="$(mktemp /tmp/harnes-videolist.XXXXXX)"
    if [ -d "$DIR" ]; then
        for f in "$DIR"/*.[mM][pP]4 "$DIR"/*.[mM][oO][vV] "$DIR"/*.[mM]4[vV] "$DIR"/*.[mM][kK][vV]; do
            [ -f "$f" ] && echo "$f"
        done > "$LIST"
    fi
    echo "== Video rekaman tersedia =="
    count=$(wc -l < "$LIST" | tr -d ' ')
    if [ "$count" = "0" ]; then
        echo "  (folder kosong: $DIR — pakai 'm' untuk path manual)"
    else
        n=0
        while IFS= read -r f; do
            n=$((n + 1))
            printf ' %2d) %-58s %s\n' "$n" "${f##*/}" "$(du -h "$f" | cut -f1)"
        done < "$LIST"
    fi
    echo '  m) path file manual (di luar folder recordings)'
    echo '  s) NYALAKAN 2 SITE SEKALIGUS (timur+depo, auto-daftar ke DB)'
    echo '  x) BUANG stream dari DB + stop (nama stream, mis. site-barat)'
    printf 'Pilih nomor video (enter = batal): '
    read -r PICK || PICK=''
    FILE=''
    case "${PICK:-}" in
        '') : ;;
        s|S) bulk_start_videos "$DIR" ;;
        x|X) printf 'Nama stream yang dibuang (mis. site-barat): '
             read -r XNAME || XNAME=''
             [ -n "$XNAME" ] && unregister_rtsp "$XNAME" ;;
        m|M) printf 'Path file video: '
             read -r FILE || FILE=''
             [ -n "$FILE" ] || FILE='' ;;
        *[!0-9]*) echo 'Nomor tidak valid.' ;;
        *)  [ -n "$PICK" ] && [ "$PICK" -ge 1 ] && [ "$PICK" -le "$count" ] \
                && FILE=$(sed -n "${PICK}p" "$LIST") \
                || echo 'Nomor di luar daftar.' ;;
    esac
    rm -f "$LIST"
    if [ -n "$FILE" ] && [ ! -f "$FILE" ]; then
        echo "File tidak ditemukan: $FILE"
        FILE=''
    fi
}

bulk_start_videos() {   # $1 = folder recordings — 3 site sekaligus
    DIR="$1"
    printf 'Resolusi: 1) 720p ringan (default)  2) asli/4K (berat di encoder): '
    read -r RES || RES=''
    for short in site-timur site-depo; do
        case "$short" in
            site-timur) pat='pexels-tom-fisk' ;;
            site-depo)  pat='pexels-ye-qiao' ;;
        esac
        file=''
        if [ -d "$DIR" ]; then
            if [ "$RES" = "2" ]; then
                file=$(ls "$DIR/$pat"*.mp4 2>/dev/null | grep -v 720p | head -1 || true)
            else
                file=$(ls "$DIR/$pat"*.mp4 2>/dev/null | grep 720p | head -1 || true)
            fi
        fi
        if [ -z "$file" ]; then
            echo "! video untuk '$short' tidak ketemu di $DIR — dilewati."
            continue
        fi
        info=$(preset_of "$file")
        IFS='|' read -r _short disp loc <<EOF
$info
EOF
        echo
        echo "== $short ← ${file##*/} =="
        sh "$SCRIPT_DIR/start-video-rtsp.sh" "$short" "$file" \
            || echo "(produser $short sudah jalan / gagal — lanjut daftar saja)"
        register_rtsp "$disp" "$loc" "$(rtsp_url "$short")"
    done
}

start_video_rtsp() {
    DIR="${HARNES_VIDEO_DIR:-$SCRIPT_DIR/../recordings}"
    choose_video_file "$DIR"
    [ -n "$FILE" ] || return 0
    IFS='|' read -r SHORT PRETTY LOC <<EOF
$(preset_of "$FILE")
EOF
    printf 'Nama singkat stream [%s] (enter = saran): ' "$SHORT"
    read -r NAMA || NAMA=''
    [ -n "$NAMA" ] || NAMA="$SHORT"
    echo
    sh "$SCRIPT_DIR/start-video-rtsp.sh" "$NAMA" "$FILE" \
        && register_rtsp "$PRETTY" "$LOC" "$(rtsp_url "$NAMA")"
}

# produser: daftar & stop
list_producers() {
    echo '-- Device kamera terpasang di Mac (bukan produser aktif):'
    run sh "$SCRIPT_DIR/start-mac-cam-rtsp.sh" --list-clean
    echo '-- Produser video RTSP yang jalan:'
    run sh "$SCRIPT_DIR/start-video-rtsp.sh" --list
}

producer_menu() {
    echo 'Produser RTSP:'
    echo ' 1) daftar semua yang jalan'
    echo ' 2) stop kamera Mac/iPhone'
    echo ' 3) stop SATU stream video (masukkan nama)'
    echo ' 4) stop SEMUA stream video'
    echo ' 0) batal'
    printf 'Pilih: '
    read -r SUB || return 0
    case "$SUB" in
        1) list_producers ;;
        2) run sh "$SCRIPT_DIR/start-mac-cam-rtsp.sh" --stop ;;
        3) printf 'Nama singkat: '
           read -r NAMA || return 0
           [ -n "$NAMA" ] && run sh "$SCRIPT_DIR/start-video-rtsp.sh" --stop "$NAMA" ;;
        4) run sh "$SCRIPT_DIR/start-video-rtsp.sh" --stop-all ;;
        *) return 0 ;;
    esac
}

# RTSP URL source (CCTV IP asli + kamera Mac/iPhone + video lokal)
rtsp_sim_menu() {
    echo '== RTSP URL Source =='
    echo ' 1. CCTV IP'
    echo ' 2. Kamera Mac/iPhone'
    echo ' 3. Local Video Input'
    echo ' 4. Produser RTSP: daftar / stop'
    echo ' 0. kembali'
    printf 'Pilih: '
    read -r SUB || return 0
    case "$SUB" in
        1) run sh "$SCRIPT_DIR/setup-cctv-rtsp.sh" ;;
        2) start_mac_cam ;;
        3) start_video_rtsp ;;
        4) producer_menu ;;
        *) return 0 ;;
    esac
}

# aksi re-login simulasi operator (keputusan owner 14 Nov 2026): hidupkan
# kembali gate operating tanpa device — logika di Tools/relogin_demo.py.
ops_relogin() {
    if ! nc -z localhost "$PORT" 2>/dev/null; then
        echo '! Server tidak jalan — nyalakan dulu (opsi 1).'
        return 1
    fi
    run sh -c 'cd "$1/.." && .venv/bin/python Tools/relogin_demo.py --port "$2"' \
        sh "$SCRIPT_DIR" "$PORT"
}

# Submenu "Demo" — satu pintu uji: SIAP (demo-ready) dan STOP (demo-stop).
demo_menu() {
    echo '== Demo =='
    echo ' 1. Demo-ready'
    echo ' 2. Demo-stop'
    echo ' 0. kembali'
    printf 'Pilih: '
    read -r SUB || return 0
    case "$SUB" in
        1) demo_up ;;
        2) demo_down ;;
        *) return 0 ;;
    esac
}

# Demo-ready — SATU PINTU dewi × Site Depo / Ye Qiao:
# produser TUNGGAL site-depo + server + register idempoten + relogin
# --operator dewi + PRE-FLIGHT terukur. Mode konsol B: setelah semua SIAP,
# server dinyalakan ulang TERSAMBUNG — log mentah sistem langsung mengalir
# ke konsol Ctrl+C menghentikan semuanya (semantik global ^C).
# Nol-ke-demo dalam sekali jalan: prasyarat (langkah 0) memasang dependensi,
# setup DB, dan video bila kosong — lalu reset demo (konfirmasi, destruktif).
# Env: HARNES_DEMO_OPERATOR (dewi) · HARNES_DEMO_CAMERA (Site Depo) ·
#      HARNES_DEMO_SCENARIO dev|demo|real (reset eksplisit; server wajib mati).
demo_up() {
    OP="${HARNES_DEMO_OPERATOR:-dewi}"
    CAMNAME="${HARNES_DEMO_CAMERA:-Site Depo}"
    echo "== DEMO-READY: $OP x $CAMNAME (pintu uji tunggal) =="
    if server_pid >/dev/null 2>&1; then
        echo "! Server sudah jalan (pid $(server_pid)) — jalankan Demo-stop"
        echo '  dulu supaya demo deterministik, lalu ulangi Demo.'
        return 1
    fi
    echo '-- 0/4 prasyarat sekali-jalan (idempoten — aman diulang)'
    MISSING=''
    for tool in ffmpeg mediamtx; do
        command -v "$tool" >/dev/null 2>&1 || MISSING="$MISSING $tool"
    done
    if [ -n "$MISSING" ]; then
        echo "! belum terpasang:$MISSING"
        if command -v brew >/dev/null 2>&1 \
            && confirm "Pasang otomatis via brew install$MISSING?"; then
            brew install $MISSING || {
                echo '  brew install gagal — pasang manual lalu ulangi Demo.'; return 1; }
        else
            echo "  Pasang manual: brew install$MISSING — lalu ulangi Demo."
            return 1
        fi
    fi
    run sh "$SCRIPT_DIR/setup-database.sh"   # idempoten: .venv + Postgres + akun DB
    if [ ! -x "$SCRIPT_DIR/../.venv/bin/python" ]; then
        echo '! setup-database belum selesai — .venv tak ada. Bereskan pesan di atas, lalu ulangi Demo.'
        return 1
    fi
    DEMO_DIR="${HARNES_VIDEO_DIR:-$SCRIPT_DIR/../recordings}"
    mkdir -p "$DEMO_DIR"
    if ! find "$DEMO_DIR" -maxdepth 1 -name 'pexels-ye-qiao*.mp4' -print -quit \
            | grep -q .; then
        echo '   video Ye Qiao / Site Depo belum ada — mengunduh sampel (sekali saja)…'
        if command -v gh >/dev/null 2>&1; then
            gh release download recordings-720p --repo akhnafal-aban/HARNES-Server -D "$DEMO_DIR" -p 'pexels-ye-qiao*' || {
                echo '   unduhan gagal — cek koneksi/gh auth, lalu ulangi Demo.'; return 1; }
        else
            echo '   tanpa gh: unduh manual dari rilis recordings-720p'
            echo '   (https://github.com/akhnafal-aban/HARNES-Server/releases/tag/recordings-720p)'
            echo "   → taruh di $DEMO_DIR — lalu ulangi Demo."
            return 1
        fi
    fi
    if [ -n "${HARNES_DEMO_SCENARIO:-}" ]; then
        SC="$HARNES_DEMO_SCENARIO"
        case "$SC" in real|prod) SC=real ;; dev|demo) ;; *)
            echo "HARNES_DEMO_SCENARIO tidak valid: '$SC' (harus dev|demo|real)." >&2
            return 1 ;; esac
        run sh "$SCRIPT_DIR/reset-database.sh" "$SC"
    elif confirm 'Reset database ke skenario DEMO (1 kamera Site Depo / Ye Qiao + dewi)? SEMUA tabel domain di-DROP.'; then
        run sh "$SCRIPT_DIR/reset-database.sh" demo
    else
        echo '   (lanjut dengan database yang ada — akun/kamera/unit demo harus sudah tersedia)'
    fi
    echo "-- 1/4 produser video RTSP: HANYA site-depo / Ye Qiao (bersih dulu)"
    sh "$SCRIPT_DIR/start-video-rtsp.sh" --stop-all >/dev/null 2>&1
    file=$(find "$DEMO_DIR" -maxdepth 1 -name 'pexels-ye-qiao*720p*.mp4' 2>/dev/null | head -1)
    [ -z "$file" ] && \
        file=$(find "$DEMO_DIR" -maxdepth 1 -name 'pexels-ye-qiao*.mp4' 2>/dev/null | head -1)
    if [ -z "$file" ]; then
        echo "! video Ye Qiao (Site Depo) tidak ketemu di $DEMO_DIR — ulangi Demo (unduhan di langkah 0 gagal?)."
        return 1
    fi
    sh "$SCRIPT_DIR/start-video-rtsp.sh" site-depo "$file" \
        || echo '   (produser site-depo sudah jalan — lanjut)'
    echo "-- 2/4 server sementara + daftar kamera + relogin $OP + PRE-FLIGHT"
    nohup sh "$SCRIPT_DIR/start-site-server.sh" >/tmp/harnes-server-demo.log 2>&1 &
    i=0
    while [ "$i" -lt 40 ] && ! nc -z localhost "$PORT" 2>/dev/null; do
        i=$((i + 1)); sleep 1
    done
    if ! nc -z localhost "$PORT" 2>/dev/null; then
        echo '! Server tidak kunjung dengar port — cek /tmp/harnes-server-demo.log'
        return 1
    fi
    # Tunggu APLIKASI siap (bukan cuma port): model deteksi dimuat ±15-20 s,
    # dan request yang jatuh saat itu membuat "server belum stabil" (fix 19 Nov).
    i=0
    while [ "$i" -lt 60 ]; do
        if curl -s --max-time 3 "http://localhost:$PORT/health" 2>/dev/null \
                | grep -q '"ok"'; then
            break
        fi
        i=$((i + 1)); sleep 1
    done
    if ! curl -s --max-time 3 "http://localhost:$PORT/health" 2>/dev/null \
            | grep -q '"ok"'; then
        echo '! Server dengar port tapi /health takanswered — cek /tmp/harnes-server-demo.log'
        return 1
    fi
    register_rtsp "$CAMNAME" 'Halaman Depo' "$(rtsp_url site-depo)"
    # Supervisor kamera (owner 14 Nov) membangkitkan SEMUA kamera desired_on
    # saat boot — padahal demo hanya menyediakan produser site-depo. Intent
    # kamera lain dimatikan lewat jalur resmi HSE (POST stop = tulis
    # desired_on false, idempoten) dan kamera demo dipastikan desired ON;
    # tanpa ini restart mode B = spam 404/fallback tiap supervisor sekejap.
    echo "   supervisor: hanya '$CAMNAME' desired ON — kamera lain OFF"
    ( cd "$SCRIPT_DIR/.." \
      && DEMO_PORT="$PORT" DEMO_KEEP="$CAMNAME" \
         HARNES_HSE_USER="${HARNES_HSE_USER:-anything}" \
         HARNES_HSE_PASS="${HARNES_HSE_PASS:-anythingcuy06}" \
         .venv/bin/python - <<'PY' ) \
      || echo '   ! gagal set desired — lanjut (cek manual lewat app HSE)'
import json, os, urllib.request
port, keep = os.environ['DEMO_PORT'], os.environ['DEMO_KEEP']
def req(method, path, body=None, token=None):
    request = urllib.request.Request(f'http://127.0.0.1:{port}{path}', method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json',
                 **({'Authorization': 'Bearer ' + token} if token else {})})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except Exception as error:
        return getattr(error, 'code', 0), None
status, body = req('POST', '/auth/login',
                   {'username': os.environ['HARNES_HSE_USER'],
                    'password': os.environ['HARNES_HSE_PASS']})
if status != 200 or not body:
    print(f'   ! login HSE gagal (HTTP {status}) — desired tak disetel')
else:
    token = body['access_token']
    status, body = req('GET', '/cameras', token=token)
    for camera in (body or {}).get('items', []):
        if camera['name'] == keep:
            code, _ = req('POST', f"/cameras/{camera['id']}/start", token=token)
            print(f"   {keep}: desired ON (start HTTP {code})")
        elif camera.get('desiredOn'):
            code, _ = req('POST', f"/cameras/{camera['id']}/stop", token=token)
            print(f"   kamera '{camera['name']}': desired OFF (stop HTTP {code})")
PY
    # Unit + assignment (fix 19 Nov): tanpa ini relogin/preflight tak punya
    # assignment AKTIF → "0/0 operator operating" dan preflight gagal, padahal
    # semua komponennya hidup. Alur resmi app: HSE bikin unit dari kamera
    # (POST /equipment) lalu assign operator (POST /assignments).
    echo "   unit + assignment untuk '$OP' (HSE: buat unit dari kamera → assign)"
    ( cd "$SCRIPT_DIR/.." \
      && DEMO_PORT="$PORT" DEMO_CAMERA="$CAMNAME" DEMO_OPERATOR="$OP" \
         HARNES_HSE_USER="${HARNES_HSE_USER:-anything}" \
         HARNES_HSE_PASS="${HARNES_HSE_PASS:-anythingcuy06}" \
         .venv/bin/python - <<'PY' ) \
      || echo '   ! gagal buat unit/assignment — cek manual lewat app HSE'
import json, os, urllib.request
port = os.environ['DEMO_PORT']; camera_name = os.environ['DEMO_CAMERA']
operator = os.environ['DEMO_OPERATOR']
def req(method, path, body=None, token=None):
    request = urllib.request.Request(f'http://127.0.0.1:{port}{path}', method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json',
                 **({'Authorization': 'Bearer ' + token} if token else {})})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b'null')
        except Exception:
            return error.code, None
    except Exception:
        return 0, None
status, body = req('POST', '/auth/login',
                   {'username': os.environ['HARNES_HSE_USER'],
                    'password': os.environ['HARNES_HSE_PASS']})
if status != 200 or not body:
    print(f'   ! login HSE gagal (HTTP {status})')
else:
    token = body['access_token']
    status, body = req('GET', '/cameras', token=token)
    camera = next((c for c in (body or {}).get('items', [])
                   if c['name'] == camera_name), None)
    if not camera:
        print(f'   ! kamera {camera_name} tak ada — tak bisa bikin unit')
    else:
        status, body = req('POST', '/equipment',
                           {'cameraId': camera['id'], 'name': f'Excavator {camera_name}'},
                           token)
        equipment_id = (body or {}).get('equipmentInstanceId') if status == 201 else None
        if status != 201:
            status, body = req('GET', '/equipment', token=token)
            item = next((e for e in (body or {}).get('items', [])
                         if e.get('cameraId') == camera['id']), None)
            equipment_id = (item or {}).get('equipmentInstanceId')
            print(f"   unit: pakai yang sudah ada (POST /equipment HTTP {status})")
        else:
            print(f'   unit dibuat: {equipment_id}')
        if not equipment_id:
            print('   ! unit tak terbentuk — assignment dilewati')
        else:
            status, body = req('GET', '/users', token=token)
            person = next((u for u in (body or {}).get('items', [])
                           if u['username'] == operator), None)
            if not person:
                print(f"   ! operator {operator} tak ada")
            else:
                status, body = req('POST', '/assignments',
                                   {'equipmentInstanceId': equipment_id,
                                    'operatorId': person['id']}, token)
                print(f"   assignment {operator} → {equipment_id}: HTTP {status}")
PY
    run sh -c 'cd "$1/.." && .venv/bin/python Tools/relogin_demo.py \
        --port "$2" --operator "$3"' sh "$SCRIPT_DIR" "$PORT" "$OP"
    # TANPA helper run(): exit-code preflight adalah keputusan demo.
    sh -c 'cd "$1/.." && .venv/bin/python Tools/preflight.py \
        --port "$2" --camera "$3" --operator "$4"' \
        sh "$SCRIPT_DIR" "$PORT" "$CAMNAME" "$OP"
    code=$?
    if [ "$code" != 0 ]; then
        echo "== BELUM SIAP — perbaiki baris ✗ di atas lalu ulangi Demo =="
        echo '   (server dibiarkan hidup untuk inspeksi: tail -f /tmp/harnes-server-demo.log)'
        return "$code"
    fi
    echo '-- push iOS/Watch (info — DILUAR demo otomatis, butuh 2 langkah manual)'
    ( cd "$SCRIPT_DIR/.." \
      && DEMO_PORT="$PORT" DEMO_OPERATOR="$OP" \
         .venv/bin/python - <<'PY' ) || true
import json, os, urllib.request
port, operator = os.environ['DEMO_PORT'], os.environ['DEMO_OPERATOR']
print(f"   mode APNs server: {os.environ.get('APNS_MODE', 'mock')} "
      "(mock = push TIDAK keluar ke Apple; prod butuh key .p8 + env APNS_*)")
try:
    from Sources.infrastructure.database.connection import connect
    with connect() as connection:
        user = connection.execute("SELECT id FROM users WHERE lower(username)=%s",
                                  (operator.lower(),)).fetchone()
        n = connection.execute("SELECT count(*) FROM device_tokens WHERE operator_id=%s",
                               (user[0],)).fetchone()[0] if user else 0
    if n:
        print(f"   device {operator}: {n} token terdaftar — push BISA sampai ke iPhone/Watch fisik")
    else:
        print(f"   device {operator}: BELUM ada token — push TIDAK sampai fisik.")
        print("   cara: install app di iPhone fisik → login sebagai operator → izinkan notifikasi")
        print("   (app otomatis POST /me/device; token tersimpan di device_tokens)")
except Exception as error:
    print(f"   (cek device dilewati: {error})")
PY
    echo "-- 3/4 mode B: LOG SISTEM MENTAH KE KONSOL --"
    if [ -t 1 ]; then
        # Tukar server sementara → TERSAMBUNG penuh: log mentah mengalir ke
        # konsol. exec: harnes digantikan server; satu Ctrl+C (trap INT server)
        # menghentikan semuanya + harnes selesai — semantik global ^C dipertahankan.
        pkill -f 'start-site-server' 2>/dev/null || true
        pkill -f 'Sources\.main' 2>/dev/null || true
        n=0
        while server_pid >/dev/null 2>&1 && [ "$n" -lt 15 ]; do
            sleep 1; n=$((n + 1))
        done
        LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || true)
        echo
        echo '========= LOG SISTEM LANGSUNG — Ctrl+C: hentikan semua & keluar harnes ========='
        [ -n "$LAN_IP" ] && echo "    iPhone: http://$LAN_IP:$PORT/   ·   Mac: http://localhost:$PORT/"
        echo '==============================================================================='
        exec sh "$SCRIPT_DIR/start-site-server.sh"
    fi
    # Non-TTY (agent/CI): ganti boot juga — server temporer menyala SEBELUM
    # desired kamera dirapikan; boot ulang supaya supervisor mulai dari state
    # bersih (desired cuma kamera demo), lalu ditinggalkan detached.
    pkill -f 'start-site-server' 2>/dev/null || true
    pkill -f 'Sources\.main' 2>/dev/null || true
    n=0
    while server_pid >/dev/null 2>&1 && [ "$n" -lt 15 ]; do
        sleep 1; n=$((n + 1))
    done
    nohup sh "$SCRIPT_DIR/start-site-server.sh" >>/tmp/harnes-server-demo.log 2>&1 &
    i=0
    while [ "$i" -lt 40 ] && ! nc -z localhost "$PORT" 2>/dev/null; do
        i=$((i + 1)); sleep 1
    done
    echo '   non-TTY: server boot ulang (state bersih) lalu detached — log: tail -f /tmp/harnes-server-demo.log'
    return "$code"
}

# Mirror demo_up: matikan SEMUA komponen demo dalam satu langkah — tanpa
# konfirmasi (kill switch), tanpa menyentuh database (registrasi kamera dan
# akun tetap; demo_up berikutnya langsung pakai yang sama).
demo_down() {
    echo '== DEMO-STOP: mematikan semua komponen demo =='
    echo '-- 1/3 produser video RTSP'
    sh "$SCRIPT_DIR/start-video-rtsp.sh" --stop-all
    echo '-- 2/3 produser kamera Mac/iPhone'
    sh "$SCRIPT_DIR/start-mac-cam-rtsp.sh" --stop
    echo '-- 3/3 server site (watchdog dulu — cegah respawn — baru python)'
    pkill -f 'start-site-server' 2>/dev/null || true
    pkill -f 'Sources\.main' 2>/dev/null || true
    n=0
    while server_pid >/dev/null 2>&1 && [ "$n" -lt 10 ]; do
        sleep 1
        n=$((n + 1))
        [ "$n" -eq 5 ] && pkill -9 -f 'Sources\.main' 2>/dev/null
    done
    server_pid >/dev/null 2>&1 \
        && echo '   ! server masih hidup — ulangi demo-stop' \
        || echo '   server mati total'
    echo '== SELESAI: produser + kamera + server berhenti =='
}

# database
db_menu() {
    echo '== Database =='
    echo ' 1. Database Setup           (sekali per Mac: PostgreSQL + .venv + migrasi)'
    echo ' 2. Database Migrate         (skema saja; aman, tanpa reset)'
    echo ' 3. Database Migrate --seed-demo (DROP + 1 kamera Site Depo / Ye Qiao + dewi)'
    echo ' 4. Database Migrate --seed-prod (DROP semua tabel + akun saja, real)'
    echo ' 5. Database Backup'
    echo ' 0. kembali'
    printf 'Pilih: '
    read -r SUB || return 0
    case "$SUB" in
        1) run sh "$SCRIPT_DIR/setup-database.sh" ;;
        2) db_migrate ;;
        3) confirm 'Migrate --seed-demo: SEMUA tabel domain di-DROP (1 kamera + dewi). Lanjut?' \
              && run sh "$SCRIPT_DIR/reset-database.sh" demo ;;
        4) confirm 'Migrate --seed-prod: SEMUA tabel domain di-DROP (data hilang). Lanjut?' \
              && run sh "$SCRIPT_DIR/reset-database.sh" prod ;;
        5) printf 'File backup (path absolut, HARUS BARU): '
           read -r OUT || OUT=''
           [ -n "$OUT" ] && run sh "$SCRIPT_DIR/backup-database.sh" "$OUT" \
               || echo 'Path kosong — dibatalkan.' ;;
        *) return 0 ;;
    esac
}

db_migrate() {
    run sh -c 'cd "$1/.." && .venv/bin/python -m Sources.infrastructure.database.migrate' \
        sh "$SCRIPT_DIR"
}

menu() {
    while :; do
        show_status
        echo ' D) Demo'
        echo ' 1) Start server'
        echo ' 2) Stop server'
        echo ' 3) RTSP URL Source'
        echo ' 4) Database'
        echo ' 0) Cek status & health detail'
        echo ' q) Keluar'
        echo ''
        echo ' Catatan: Ctrl+C = hentikan server/aksi dan keluar.'
        printf 'Pilih: '
        read -r PILIH || break
        case "$PILIH" in
            1) start_server ;;
            2) stop_server ;;
            3) rtsp_sim_menu ;;
            4) db_menu ;;
            Demo|demo|D) demo_menu ;;
            0) list_producers ;;
            q|Q|x|X|exit) break ;;
            *) echo 'Pilihan tidak dikenal.' ;;
        esac
        printf '\n\n'   # pemisah: hasil aksi vs panel berikutnya
    done
    echo 'Sampai jumpa.'
}

usage() {
    echo 'HARNES — gate script. Pintu utama: sh Scripts/harnes.sh  (menu interaktif)'
    echo 'Perintah langsung:'
    echo '  server | server-stop | kamera … | kamera-list | cctv … | video <nama> [file] | video-list'
    echo '  video-stop <nama> | video-stop-all | db-setup | db-migrate | db-reset dev|prod'
    echo '  ops-relogin | demo | demo-stop | db-backup <path> | status'
    echo '  cctv … = setup CCTV IP asli (probe → daftar → verifikasi; --list/--remove/--probe)'
}

# eksekusi
case "${1:-}" in
    '')  if [ -t 0 ]; then
             menu
         else
             echo 'Menu interaktif butuh terminal — jalankan dari Terminal.app.' >&2
             usage >&2
             exit 1
         fi ;;
    server)         run sh "$SCRIPT_DIR/start-site-server.sh" ;;
    server-stop)    stop_server ;;
    kamera)         shift; run sh "$SCRIPT_DIR/start-mac-cam-rtsp.sh" "$@" ;;
    cctv)           shift; run sh "$SCRIPT_DIR/setup-cctv-rtsp.sh" "$@" ;;
    kamera-list)    run sh "$SCRIPT_DIR/start-mac-cam-rtsp.sh" --list ;;
    video-list)     run sh "$SCRIPT_DIR/start-video-rtsp.sh" --list ;;
    video-stop)     shift; run sh "$SCRIPT_DIR/start-video-rtsp.sh" --stop "$@" ;;
    video-stop-all) run sh "$SCRIPT_DIR/start-video-rtsp.sh" --stop-all ;;
    video)
        shift
        vname="${1:-}"
        if sh "$SCRIPT_DIR/start-video-rtsp.sh" "$@"; then
            # Hanya START (bukan --list/--stop) yang mendaftarkan kamera ke DB;
            # produser gagal juga tidak boleh membuat baris kamera sampah.
            case "$vname" in
                ''|--*|help) : ;;
                *) register_rtsp "Video $vname" 'Rekaman video' "$(rtsp_url "$vname")" ;;
            esac
        fi ;;
    db-setup)    run sh "$SCRIPT_DIR/setup-database.sh" ;;
    db-migrate)  db_migrate ;;
    db-reset)    shift; run sh "$SCRIPT_DIR/reset-database.sh" "$@" ;;
    db-backup)   shift; run sh "$SCRIPT_DIR/backup-database.sh" "$@" ;;
    ops-relogin) ops_relogin ;;
    demo)        demo_up ;;
    demo-stop)   demo_down ;;
    status)      show_status ;;
    help|-h|--help) usage ;;
    *) echo "Perintah tidak dikenal: $1" >&2; usage >&2; exit 1 ;;
esac
