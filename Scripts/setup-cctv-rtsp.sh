#!/bin/bash
# Konfigurasi CCTV IP asli (kamera RTSP di jaringan lokal) untuk server HARNES.
#
# Alur: Cecilkan kredensial + path -> PROBE kamera (ffprobe) -> daftar ke DB
# server (POST /cameras type=rtsp) -> verifikasi frame benar-benar mengalir.
#
# Intake LANGSUNG: server HARNES yang menarik rtsp://user:pass@kamera:554/path
# (fakta terkonfirmasi AGENTS.md + CLI Sources.cameras.cli yang sudah ada).
# Tanpa relay MediaMTX, tanpa jalur capture baru.
#
# Password kamera disimpan di file lokal mode 0600 (Server/.secrets/, di luar
# repo + sudah di-gitignore) supaya setup ulang tidak perlu input ulang.
# Password juga ikut tersimpan di cameras.source (PostgreSQL lokal) — itu pola
# resmi project: RTSP input disimpan di DB, bukan di repository.
#
# Pemakaian:
#   sh Scripts/setup-cctv-rtsp.sh              # interaktif (probe → daftar → verifikasi)
#   sh Scripts/setup-cctv-rtsp.sh --probe      # uji kamera saja (tanpa daftar)
#   sh Scripts/setup-cctv-rtsp.sh --discover   # cari kamera di jaringan (port 554/443)
#   sh Scripts/setup-cctv-rtsp.sh --list       # daftar kamera CCTV + status
#   sh Scripts/setup-cctv-rtsp.sh --remove <id-prefix>
#
# Argumen non-interaktif:
#   --host IP --port 554 --user admin --pass SECRET --path /stream1
#   --name "Nama Kamera" --location "Lokasi" --no-save
#
# Dependensi: ffprobe (brew install ffmpeg), server HARNES jalan (port 8080).
set -euo pipefail

PORT_DEFAULT=554
PROBE_TIMEOUT_US=6000000
SERVER_PORT="${HARNES_SERVER_PORT:-8080}"
HSE_USER="${HARNES_HSE_USER:-anything}"
HSE_PASS="${HARNES_HSE_PASS:-anythingcuy06}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_DIR="$SCRIPT_DIR/.."
SECRET_DIR="$SERVER_DIR/.secrets"
PYBIN="$SERVER_DIR/.venv/bin/python"

# Kandidat path RTSP — urut dari yang paling umum. 401 = kredensial ditolak
# (kamera Tapo/Hikvision menolak SEMUA path saat kredensial salah, jadi 401
# di path manapun = kredensial, bukan path); 404 = path tidak ada, coba next.
# Array (bukan string) supaya '?' & '&' tidak jadi glob saat di-expand.
PATH_CANDIDATES=("/stream1" "/stream2" "/Streaming/Channels/101" "/Streaming/Channels/102" \
"/cam/realmonitor?channel=1&subtype=0" "/cam/realmonitor?channel=1&subtype=1" \
"/live/ch0" "/h264" "/media/video1" "/onvif1" "/video1")

fail() { echo "$1" >&2; exit 1; }
say()  { echo "$*"; }

slugify() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]' \
        | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//'
}

# Password bisa berisi karakter khusus (@ : / ? # & %) — di URL RTSP harus
# di-encode, kalau tidak URL-nya patah dan kameranya dapat kredensial salah.
urlencode() {
    printf '%s' "$1" | sed -E 's/%/%25/g; s/@/%40/g; s/:/%3A/g; s/\//%2F/g; s/\?/%3F/g; s/#/%23/g; s/&/%26/g; s/ /%20/g'
}

rtsp_url() {  # $1 host, $2 port, $3 user, $4 pass, $5 path
    printf 'rtsp://%s:%s@%s:%s%s' "$(urlencode "$3")" "$(urlencode "$4")" "$1" "$2" "$5"
}

mask_url() {  # sembunyikan password pada URL untuk ditampilkan
    printf '%s' "$1" | sed -E 's#(rtsp://[^:/@]+):[^@]*@#\1:***@#'
}

# Terjemahkan hasil probe → pesan yang bisa ditindaklanjuti.
explain_probe() {
    case "$1" in
        AUTH) echo "kredensial ditolak (401) — cek username/password kamera" ;;
        404) echo "path RTSP tidak ada (404)" ;;
        TIMEOUT) echo "kamera tidak terjangkau (timeout — cek IP, jaringan, dan port 554)" ;;
        *) echo "$1" ;;
    esac
}

wait_for_api() {
    local attempt=0 code=""
    while [ "$attempt" -lt 15 ]; do
        code="$(curl -sS --max-time 2 -o /dev/null -w '%{http_code}' \
            "http://127.0.0.1:$SERVER_PORT/health" 2>/dev/null || true)"
        [ "$code" = "200" ] && return 0
        attempt=$((attempt + 1))
        [ "$attempt" -eq 1 ] && \
            echo "Menunggu API HARNES di port $SERVER_PORT siap..." >&2
        sleep 1
    done
    echo "Server HARNES tidak siap di port $SERVER_PORT (health HTTP ${code:-000})." >&2
    echo "Nyalakan dulu: sh Scripts/harnes.sh → 1) Start server" >&2
    return 1
}

api_login() {
    wait_for_api || return 1
    local response http_status body
    response="$(printf '{"username":"%s","password":"%s"}' "$HSE_USER" "$HSE_PASS" \
        | curl -sS --max-time 8 -w '\n%{http_code}' -X POST \
            "http://127.0.0.1:$SERVER_PORT/auth/login" \
            -H 'Content-Type: application/json' --data-binary @- 2>/dev/null)" || {
        echo "API HARNES terputus saat login." >&2
        return 1
    }
    http_status="${response##*$'\n'}"
    body="${response%$'\n'*}"
    if [ "$http_status" != "200" ]; then
        if [ "$http_status" = "401" ]; then
            echo "Login HSE ditolak untuk user '$HSE_USER' — periksa akun/password server." >&2
        else
            echo "Login API HARNES gagal (HTTP $http_status)." >&2
        fi
        return 1
    fi
    printf '%s' "$body" | "$PYBIN" -c 'import json,sys
try:
    print(json.load(sys.stdin)["access_token"])
except Exception:
    sys.exit(1)'
}

api_cameras() {  # $1 token → JSON daftar kamera
    curl -s --max-time 10 -H "Authorization: Bearer $1" \
        "http://127.0.0.1:$SERVER_PORT/cameras"
}

api_snapshot_etag() {  # $1 token, $2 camera id → ETag frame terbaru
    curl -sS --max-time 5 -D - -o /dev/null \
        -H "Authorization: Bearer $1" \
        "http://127.0.0.1:$SERVER_PORT/cameras/$2/snapshot.jpg" 2>/dev/null \
        | awk 'BEGIN{IGNORECASE=1} /^ETag:/{gsub("\r", ""); print $2; exit}'
}

# ffprobe 1 path → "OK <codec> <w>x<h> <fps>" | "AUTH" | "404" | "TIMEOUT"
probe_path() {  # $1 url
    local out
    out="$(ffprobe -v error -rtsp_transport tcp -timeout "$PROBE_TIMEOUT_US" \
        -show_entries stream=codec_name,width,height,avg_frame_rate \
        -of default=noprint_wrappers=1 "$1" 2>&1 || true)"
    if printf '%s' "$out" | grep -q 'codec_name='; then
        local codec width height fps
        codec="$(printf '%s' "$out" | awk -F= '/^codec_name=/{print $2; exit}')"
        width="$(printf '%s' "$out" | awk -F= '/^width=/{print $2; exit}')"
        height="$(printf '%s' "$out" | awk -F= '/^height=/{print $2; exit}')"
        fps="$(printf '%s' "$out" | awk -F= '/^avg_frame_rate=/{print $2; exit}')"
        echo "OK $codec ${width:-?}x${height:-?} ${fps:-?}"
        return 0
    fi
    if printf '%s' "$out" | grep -qi '401\|Unauthorized'; then
        echo "AUTH"; return 0
    fi
    if printf '%s' "$out" | grep -qi '404\|Not Found'; then
        echo "404"; return 0
    fi
    echo "TIMEOUT"; return 0
}

# Cari perangkat dengan port RTSP/HTTPS terbuka di /24 jaringan lokal.
discover() {
    local base prefix
    base="$(ipconfig getifaddr en0 2>/dev/null || echo '')"
    [ -n "$base" ] || fail "Tidak bisa tahu IP lokal (en0). ISI manual."
    prefix="${base%.*}"
    say "Memindai ${prefix}.0/24 (port 554 + 443)..."
    for octet in $(seq 1 254); do echo "$prefix.$octet"; done \
        | xargs -P 64 -I{} sh -c '
            nc -z -G 1 "{}" 554 >/dev/null 2>&1 && echo "RTSP {}"
            nc -z -G 1 "{}" 443 >/dev/null 2>&1 && echo "HTTPS {}"' 2>/dev/null
    say "Selesai. (IP CCTV bisa berubah tiap DHCP — jalankan --discover lagi bila tidak ketemu.)"
}

cmd_list() {
    [ -x "$PYBIN" ] || fail ".venv server belum ada — jalankan: sh Scripts/harnes.sh db-setup"
    command -v curl >/dev/null 2>&1 || fail "curl belum ada"
    local token
    token="$(api_login || true)"
    [ -n "$token" ] || fail "Tidak bisa membaca daftar kamera: autentikasi API gagal."
    api_cameras "$token" | "$PYBIN" -c '
import json, sys, time, re
items = json.load(sys.stdin).get("items", [])
now = int(time.time() * 1000)
rows = [c for c in items if str(c.get("source", "")).startswith("rtsp://")]
if not rows:
    print("Belum ada kamera RTSP terdaftar.")
for c in rows:
    src = c.get("source", "")
    if "127.0.0.1" in src or "localhost" in src:
        continue  # produser lokal (video/mac) — bukan CCTV IP
    src = re.sub(r"(rtsp://[^:/@]+):[^@]*@", r"\1:***@", src)
    age = (now - (c.get("lastSeenMs") or 0)) / 1000
    print("{}  {:<8} {:<30} {:<18} lastSeen {:6.1f}s lalu  {}".format(
        c["id"], c["status"], c["name"], c.get("location") or "-", age, src))
'
}

cmd_remove() {
    local target="${1:-}"
    [ -n "$target" ] || fail "Pakai: $0 --remove <id-prefix>"
    [ -x "$PYBIN" ] || fail ".venv server belum ada"
    local token id
    token="$(api_login || true)"
    [ -n "$token" ] || fail "Tidak bisa menghapus kamera: autentikasi API gagal."
    id="$(api_cameras "$token" | "$PYBIN" -c '
import json, sys
target = sys.argv[1]
items = json.load(sys.stdin).get("items", [])
hits = [c["id"] for c in items if c["id"].startswith(target)]
if len(hits) == 1:
    print(hits[0])
elif not hits:
    sys.exit(3)
else:
    sys.exit(4)
' "$target" || true)"
    if [ -z "$id" ]; then
        fail "Kamera '$target' tidak ditemukan (atau prefix ambigu)."
    fi
    curl -s --max-time 10 -o /dev/null -w '%{http_code}\n' -X DELETE \
        "http://127.0.0.1:$SERVER_PORT/cameras/$id" -H "Authorization: Bearer $token" \
        | grep -q '^200$' || fail "Hapus kamera gagal."
    say "Kamera '$target' dihapus (kamera IP_camera dihapus dari daftar; file kredensial lokal tetap ada)."
}

# ---------------------------------------------------------------- mode setup
cmd_setup() {
    local host="" port="$PORT_DEFAULT" user="" pass="" path="" name="" location="" slug="" no_save=0
    local probe_only=0 do_discover=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --host) host="${2:-}"; shift 2 ;;
            --port) port="${2:-}"; shift 2 ;;
            --user) user="${2:-}"; shift 2 ;;
            --pass) pass="${2:-}"; shift 2 ;;
            --path) path="${2:-}"; shift 2 ;;
            --name) name="${2:-}"; shift 2 ;;
            --location) location="${2:-}"; shift 2 ;;
            --slug) slug="${2:-}"; shift 2 ;;
            --no-save) no_save=1; shift ;;
            --probe) probe_only=1; shift ;;
            --discover) do_discover=1; shift ;;
            *) fail "Argumen tak dikenal: $1" ;;
        esac
    done

    command -v ffprobe >/dev/null 2>&1 || fail "ffprobe belum ada — brew install ffmpeg"
    [ -x "$PYBIN" ] || fail ".venv server belum ada — jalankan: sh Scripts/harnes.sh db-setup"

    [ "$do_discover" -eq 1 ] && discover

    say "== CCTV IP asli — setup =="
    if [ -z "$host" ]; then
        printf 'IP/host kamera [172.16.0.83]: '
        read -r host || fail "Input dibatalkan."
        host="${host:-172.16.0.83}"
    fi
    if [ -z "$user" ]; then
        printf 'Username kamera [admin]: '
        read -r user || fail "Input dibatalkan."
        user="${user:-admin}"
    fi
    if [ -z "$pass" ]; then
        printf 'Password kamera (tidak ditampilkan): '
        read -rs pass || fail "Input dibatalkan."
        printf '\n'
    fi
    [ -n "$pass" ] || fail "Password kosong — kamera butuh kredensial (401 tanpa password)."

    # PROBE: coba path kandidat; AUTH = kredensial salah (fatal), 404 = next path.
    if [ -z "$path" ]; then
        say "Mencoba path RTSP di $host:$port ..."
        local found=""
        for candidate in "${PATH_CANDIDATES[@]}"; do
            local res
            res="$(probe_path "$(rtsp_url "$host" "$port" "$user" "$pass" "$candidate")")"
            case "$res" in
                OK*) found="$candidate"; say "  ✓ $candidate → ${res#OK }"; break ;;
                AUTH) fail "Kredensial ditolak (401) untuk $host — cek username/password. (Path tidak relevan: kamera menolak semua path saat kredensial salah.)" ;;
                404) say "  · $candidate → 404 (path tidak ada)" ;;
                *) say "  · $candidate → $(explain_probe "$res")" ;;
            esac
        done
        if [ -n "$found" ]; then
            path="$found"
            say "Path dipakai: $path"
        else
            printf 'Path RTSP manual (contoh /stream1): '
            read -r path || fail "Input dibatalkan."
            [ -n "$path" ] || fail "Path wajib diisi."
            local res2
            res2="$(probe_path "$(rtsp_url "$host" "$port" "$user" "$pass" "$path")")"
            case "$res2" in
                OK*) say "  ✓ $path → ${res2#OK }" ;;
                *) fail "Path '$path' gagal: $(explain_probe "$res2")" ;;
            esac
        fi
    else
        local res3
        res3="$(probe_path "$(rtsp_url "$host" "$port" "$user" "$pass" "$path")")"
        case "$res3" in
            OK*) say "  ✓ $path → ${res3#OK }" ;;
            *) fail "Path '$path' gagal: $(explain_probe "$res3")" ;;
        esac
    fi

    if [ "$probe_only" -eq 0 ]; then
        if [ -z "$name" ]; then
            printf 'Nama tampilan di app [CCTV %s]: ' "$host"
            read -r name || fail "Input dibatalkan."
            name="${name:-CCTV $host}"
        fi
        if [ -z "$location" ]; then
            printf 'Lokasi [Area Kamera]: '
            read -r location || fail "Input dibatalkan."
            location="${location:-Area Kamera}"
        fi
    fi

    local url
    url="$(rtsp_url "$host" "$port" "$user" "$pass" "$path")"

    if [ "$probe_only" -eq 1 ]; then
        say "Mode --probe: kamera valid. Tidak didaftarkan ke database."
        return 0
    fi

    # DAFTAR: POST /cameras type=rtsp → server auto-start produser.
    local token body resp id
    token="$(api_login || true)"
    [ -n "$token" ] || fail "Kamera belum didaftarkan: autentikasi API HARNES gagal."
    body="$("$PYBIN" -c '
import json, sys
print(json.dumps({"name": sys.argv[1], "location": sys.argv[2],
                  "type": "rtsp", "source": sys.argv[3]}))' "$name" "$location" "$url")"
    resp="$(curl -s --max-time 15 -X POST "http://127.0.0.1:$SERVER_PORT/cameras" \
        -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
        --data-binary "$body")"
    id="$(printf '%s' "$resp" | "$PYBIN" -c '
import json, sys
try:
    print(json.load(sys.stdin).get("id", ""))
except Exception:
    print("")')"
    if [ -z "$id" ]; then
        # Source sudah terdaftar → cari id yang ada, lalu verifikasi kamera itu.
        if printf '%s' "$resp" | grep -q 'already_exists'; then
            local existing existing_name existing_location
            existing="$(api_cameras "$token" | "$PYBIN" -c '
import json, sys
target = sys.argv[1]
for c in json.load(sys.stdin).get("items", []):
    if c.get("source") == target:
        print(c["id"])
        print(c.get("name") or "")
        print(c.get("location") or "")
        break
' "$url" || true)"
            id="$(printf '%s\n' "$existing" | sed -n '1p')"
            [ -n "$id" ] || fail "Kamera dengan source ini sudah terdaftar tapi tak ditemukan di daftar — cek: $0 --list"
            existing_name="$(printf '%s\n' "$existing" | sed -n '2p')"
            existing_location="$(printf '%s\n' "$existing" | sed -n '3p')"
            say "Source ini sudah terdaftar sebagai '${existing_name:-$id}'${existing_location:+ di '$existing_location'} — tidak membuat kamera duplikat."
            name="${existing_name:-$name}"
            location="${existing_location:-$location}"
        else
            printf '%s' "$resp" | "$PYBIN" -c '
import json, sys
try:
    print(json.load(sys.stdin).get("error", "registrasi ditolak"))
except Exception:
    print("respons tidak valid")' >&2
            fail "Pendaftaran kamera gagal."
        fi
    else
        say "Terdaftar: $name (id $id) — server mulai menarik stream."
    fi

    # File lokal baru dianggap siap setelah API menerima kamera. Dengan urutan
    # ini, pesan "disimpan" tidak lagi terlihat seperti sukses database ketika
    # server/login gagal sebelum POST /cameras.
    if [ "$no_save" -eq 0 ]; then
        slug="${slug:-$(slugify "${name:-$host}")}"
        mkdir -p "$SECRET_DIR"
        chmod 700 "$SECRET_DIR"
        printf '%s\n' "$url" > "$SECRET_DIR/cctv-$slug.url"
        chmod 600 "$SECRET_DIR/cctv-$slug.url"
        say "Kredensial disimpan: Server/.secrets/cctv-$slug.url (mode 600, di luar repo)"
    fi

    # VERIFIKASI: ETag snapshot membawa counter frame CameraManager. Dua
    # perubahan berturut membuktikan frame baru tersedia lewat API yang sama
    # dengan frontend; lastSeenMs hanya mencatat transisi status, bukan frame.
    local waited=0 prev="" bumps=0 etag="" detail="" st="" note=""
    while [ "$waited" -lt 40 ]; do
        etag="$(api_snapshot_etag "$token" "$id" || true)"
        if [ -n "$etag" ]; then
            if [ -n "$prev" ] && [ "$etag" != "$prev" ]; then
                bumps=$((bumps + 1))
                if [ "$bumps" -ge 2 ]; then
                    say "OK — frame mengalir ke frontend (ETag berubah ${bumps}× berturut)."
                    say "   Lihat di app HSE → daftar kamera: $name"
                    return 0
                fi
            fi
            prev="$etag"
        fi
        sleep 1
        waited=$((waited + 1))
    done
    detail="$(api_cameras "$token" | "$PYBIN" -c '
import json, sys
target = sys.argv[1]
for c in json.load(sys.stdin).get("items", []):
    if c["id"] == target:
        print(c.get("status", ""), c.get("healthNote") or "")
        break
' "$id" || true)"
    st="$(printf '%s' "$detail" | awk '{print $1}')"
    note="$(printf '%s' "$detail" | cut -d' ' -f2-)"
    say "PERINGATAN: kamera terdaftar tapi frame belum mengalir dalam 40 detik."
    say "  status=${st:-—}  note=${note:-—}"
    say "  Cek: (1) kamera & server di jaringan yang sama; (2) sesi RTSP kamera tidak habis"
    say "       (kamera kecil biasanya batas 1-2 sesi); (3) ffprobe manual:"
    say "       ffprobe -rtsp_transport tcp \"$(mask_url "$url")\""
    return 1
}

# ------------------------------------------------------------------ entry
case "${1:-}" in
    --list) cmd_list ;;
    --remove) shift; cmd_remove "${1:-}" ;;
    --discover)
        shift
        cmd_setup --discover "$@" ;;
    --probe)
        shift
        cmd_setup --probe "$@" ;;
    --help|-h)
        sed -n '2,26p' "$0" ;;
    *) cmd_setup "$@" ;;
esac
