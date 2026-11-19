"""Custom-format database dump; credentials never appear in command arguments."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

from psycopg.conninfo import conninfo_to_dict

from .connection import database_url


def pg_tool(name):
    executable = shutil.which(name)
    if executable:
        return executable
    for prefix in ('/opt/homebrew', '/usr/local'):
        candidate = Path(prefix) / 'opt/postgresql@18/bin' / name
        if candidate.exists():
            return str(candidate)
    raise ValueError('Tool PostgreSQL belum tersedia di PATH.')


def pg_environment(url):
    options = conninfo_to_dict(url)
    env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
    for key, target in [('host', 'PGHOST'), ('port', 'PGPORT'), ('user', 'PGUSER'), ('password', 'PGPASSWORD'), ('dbname', 'PGDATABASE')]:
        if key in options:
            env[target] = options[key]
    env['PGCONNECT_TIMEOUT'] = '5'
    return env


def backup(destination, url=None):
    destination = Path(destination).expanduser().resolve()
    repo = Path(__file__).resolve().parents[5]
    if destination.is_relative_to(repo):
        raise ValueError('Simpan backup di luar repository.')
    env = pg_environment(url or database_url())
    command = [pg_tool('pg_dump'), '--format=custom', '--no-password']
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as output:
            subprocess.run(command, env=env, stdout=output, stderr=subprocess.PIPE, check=True)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', help='Path baru di luar repository; tidak menimpa backup lama.')
    args = parser.parse_args()
    try:
        path = backup(args.destination)
        print(f'Backup tersimpan: {path}')
    except (ValueError, OSError, subprocess.CalledProcessError):
        raise SystemExit('Backup gagal. Periksa koneksi, tool PostgreSQL, dan path baru di luar repository.') from None


if __name__ == '__main__':
    main()
