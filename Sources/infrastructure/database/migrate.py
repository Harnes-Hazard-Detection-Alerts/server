"""Transactional, checksummed SQL migrations. Never reset existing data."""
import hashlib

import psycopg

from ...config import SERVER_ROOT
from .connection import connect

MIGRATIONS = SERVER_ROOT / 'migrations'

# This migration shipped as 017 before the operator-response migrations landed,
# then was renumbered to 020. Keep both immutable files so databases created by
# either checkout remain valid, while executing their equivalent DDL only once.
EQUIVALENT_MIGRATIONS = {
    '017_watch_push_targets.sql': '020_watch_push_targets.sql',
    '020_watch_push_targets.sql': '017_watch_push_targets.sql',
}


def migrate(connection, directory=MIGRATIONS):
    with connection.transaction():
        connection.execute('SELECT pg_advisory_xact_lock(681204711)')
        connection.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
            version text PRIMARY KEY, checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now())''')
        applied = dict(connection.execute('SELECT version, checksum FROM schema_migrations').fetchall())
        files = {path.name: path for path in sorted(directory.glob('[0-9]*.sql'))}
        if set(applied) - set(files):
            raise ValueError('Migrasi yang pernah diterapkan tidak ditemukan di checkout ini.')
        checksums = {
            version: hashlib.sha256(path.read_bytes()).hexdigest()
            for version, path in files.items()
        }
        if any(applied[version] != checksums[version] for version in applied):
            raise ValueError('Migrasi yang sudah diterapkan berubah; buat migrasi baru.')
        count = 0
        for version, path in files.items():
            checksum = checksums[version]
            if version in applied:
                continue
            equivalent = EQUIVALENT_MIGRATIONS.get(version)
            if equivalent not in applied:
                connection.execute(path.read_text())
            connection.execute('INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s)', (version, checksum))
            applied[version] = checksum
            count += 1
        return count


def main():
    try:
        with connect() as connection:
            count = migrate(connection)
        print(f'Migrasi selesai: {count} migrasi baru.')
    except (ValueError, psycopg.Error):
        raise SystemExit('Migrasi gagal dan transaksi dibatalkan. Periksa koneksi serta urutan/checksum file migrasi.') from None


if __name__ == '__main__':
    main()
