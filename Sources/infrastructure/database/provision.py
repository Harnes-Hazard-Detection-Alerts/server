"""Provision the local development database using the macOS owner's PG account."""
import os
from pathlib import Path
import secrets
import time

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from ...config import ENV_FILE
from .connection import connect, database_url
from .migrate import migrate

ROLE = 'safety_app'
DATABASE = 'safety_system'
HBA_MARKER = '# safety-system local application authentication\n'
HBA_RULES = (HBA_MARKER + 'local all safety_app scram-sha-256\n'
             'host all safety_app 127.0.0.1/32 scram-sha-256\n'
             'host all safety_app ::1/128 scram-sha-256\n')


def provision():
    if 'DATABASE_URL' in os.environ:
        raise ValueError('Jalankan provisioning tanpa override DATABASE_URL; setup ini hanya untuk database lokal tetap.')
    # Admin uses the existing local Unix socket, not application credentials.
    with psycopg.connect('dbname=postgres host=/tmp connect_timeout=5', autocommit=True) as admin:
        admin.execute('SELECT pg_advisory_lock(681204712)')
        if admin.execute('SHOW listen_addresses').fetchone()[0] != 'localhost':
            raise ValueError('Setup mensyaratkan listen_addresses=localhost.')
        role = admin.execute('SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname=%s', (ROLE,)).fetchone()
        owner = admin.execute('SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s', (DATABASE,)).fetchone()
        if role and any(role):
            raise ValueError('Role safety_app yang ada memiliki hak istimewa; periksa manual.')
        if owner and owner[0] != ROLE:
            raise ValueError('Database sudah dimiliki akun lain; setup tidak mengambil alih data.')
        if not ENV_FILE.exists():
            if role or owner:
                raise ValueError('Database/role sudah ada tetapi .env hilang. Pulihkan kredensial; password tidak direset otomatis.')
            password = secrets.token_urlsafe(32)
            fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as output:
                output.write(f'DATABASE_URL=postgresql://{ROLE}:{password}@localhost:5432/{DATABASE}\n')
        options = conninfo_to_dict(database_url())
        if any(options.get(key) != value for key, value in {'user': ROLE, 'dbname': DATABASE, 'host': 'localhost', 'port': '5432'}.items()):
            raise ValueError('.env tidak cocok dengan target setup lokal; file tidak ditimpa.')
        os.chmod(ENV_FILE, 0o600)
        if not role:
            admin.execute("SET password_encryption = 'scram-sha-256'")
            admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}').format(sql.Identifier(ROLE), sql.Literal(options['password'])))
        if not owner:
            admin.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(DATABASE), sql.Identifier(ROLE)))
        hba = Path(admin.execute('SHOW hba_file').fetchone()[0])
        previous = hba.read_text()
        if not previous.startswith(HBA_RULES):
            if HBA_MARKER in previous:
                raise ValueError('Blok autentikasi aplikasi telah diubah; periksa manual.')
            backup = hba.with_name('pg_hba.conf.before-safety-system')
            if backup.exists():
                raise ValueError('Backup HBA sudah ada; periksa sebelum mengubah konfigurasi lagi.')
            backup.write_text(previous)
            backup.chmod(0o600)
            hba.write_text(HBA_RULES + previous)
            if admin.execute('SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL').fetchone()[0]:
                hba.write_text(previous)
                raise ValueError('Validasi HBA gagal; konfigurasi sebelumnya dipulihkan.')
            admin.execute('SELECT pg_reload_conf()')
        # Verify rejection, not just successful connection: trust must not bypass passwords.
        for attempt in range(20):
            try:
                with psycopg.connect(database_url(), password=secrets.token_urlsafe(32), connect_timeout=2):
                    pass
            except psycopg.OperationalError as error:
                if 'password authentication failed' in str(error):
                    break
                raise
            time.sleep(0.1)
        else:
            raise ValueError('Autentikasi password belum berlaku.')
        with connect() as connection:
            connection.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
            count = migrate(connection)
        print(f'Database safety_system siap; {count} migrasi baru. Kredensial tersimpan di Server/.env.')


def main():
    try:
        provision()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except (OSError, psycopg.Error):
        raise SystemExit('Setup gagal. Periksa layanan, akses admin lokal, izin pg_hba.conf, dan kredensial .env. Data tidak direset.') from None


if __name__ == '__main__':
    main()
