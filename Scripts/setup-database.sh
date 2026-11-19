#!/bin/sh
# Repeatable local setup. Existing databases and credentials are never reset.
set -eu
cd "$(dirname "$0")/.."

if ! command -v brew >/dev/null 2>&1; then
    echo 'Homebrew belum tersedia di PATH. Instal dari https://brew.sh lalu jalankan ulang.' >&2
    exit 1
fi

for formula in postgresql@18 python@3.11; do
    if [ -z "$(brew list --versions "$formula" 2>/dev/null)" ]; then
        echo "Memasang $formula..."
        brew install "$formula"
    fi
done

pg_bin="$(brew --prefix postgresql@18)/bin"
python_bin="$(brew --prefix python@3.11)/bin/python3.11"
export PATH="$pg_bin:$PATH"

if [ ! -x .venv/bin/python ]; then
    echo 'Menyiapkan virtual environment Python...'
    "$python_bin" -m venv .venv
fi
if ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)'; then
    echo '.venv yang ada bukan Python 3.11. Periksa environment tersebut sebelum membuat ulang; skrip tidak menghapusnya.' >&2
    exit 1
fi

# Avoid contacting PyPI when the pinned requirements are already installed.
if ! .venv/bin/python - <<'PY'
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys
def _parts(text):
    # bandingkan numerik per segmen ('3.2' < '3.2.13' < '3.3.8')
    out = []
    for chunk in text.strip().split('.'):
        digits = ''.join(c for c in chunk if c.isdigit())
        out.append(int(digits) if digits else 0)
    return out

try:
    for line in Path('requirements.txt').read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        req = line.strip()
        for sep in ('==', '>='):
            if sep in req:
                name, expected = req.split(sep, 1)
                break
        else:
            continue
        package = name.split('[')[0]
        installed = _parts(version(package))
        want = _parts(expected)
        if sep == '==' and installed != want:
            sys.exit(1)
        if sep == '>=' and installed < want:
            sys.exit(1)
        if name == 'psycopg[binary]' and _parts(version('psycopg-binary')) != installed:
            sys.exit(1)
except (PackageNotFoundError, ValueError):
    sys.exit(1)
PY
then
    echo 'Memasang dependensi Python...'
    .venv/bin/python -m pip install -r requirements.txt
fi
.venv/bin/python -m pip check

brew services start postgresql@18
attempt=0
until "$pg_bin/pg_isready" -h localhost -p 5432 >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 15 ]; then
        echo 'PostgreSQL belum siap pada localhost:5432. Periksa brew services list dan konflik port.' >&2
        exit 1
    fi
    sleep 1
done

# Provisioning uses this local socket too; reject another PostgreSQL major version.
server_version=$("$pg_bin/psql" -X -w -h /tmp -d postgres -Atc 'SHOW server_version_num')
case "$server_version" in
    18[0-9][0-9][0-9][0-9]) ;;
    *) echo 'Server pada socket lokal bukan PostgreSQL 18. Periksa layanan sebelum melanjutkan.' >&2; exit 1 ;;
esac

.venv/bin/python -m Sources.infrastructure.database.provision
.venv/bin/python -m Sources.infrastructure.database.connection
echo 'Setup database selesai.'
