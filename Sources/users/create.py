"""Local operator/HSE account provisioning CLI; no public signup.

Inti pembuatan akun hidup di users/admin.py (dipakai juga endpoint
POST /users, api-draft 4.2 N2-B) — modul ini hanya shell CLI: interaksi
getpass + pemetaan error ke pesan. Exception tetap mentah (ValueError,
psycopg.errors.UniqueViolation) seperti sebelumnya.
"""
import argparse
import getpass

import psycopg

from ..infrastructure.database.connection import connect
from .admin import create_account


def create_user(username, display_name, role, password, connection_factory=connect):
    row = create_account(username, display_name, role, password, connection_factory)
    return str(row['id'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--username', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--role', required=True, choices=['operator', 'hse'])
    args = parser.parse_args()
    try:
        password = getpass.getpass('Password (12–256 karakter): ')
        if password != getpass.getpass('Ulangi password: '):
            raise ValueError('Password tidak sama.')
        user_id = create_user(args.username, args.name, args.role, password)
        print(f'Akun dibuat: {args.username} ({args.role}), id={user_id}')
    except psycopg.errors.UniqueViolation:
        raise SystemExit('Username sudah digunakan; akun lama tidak diubah.') from None
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except psycopg.Error:
        raise SystemExit('Pembuatan akun gagal. Periksa koneksi dan migrasi database.') from None
    except (KeyboardInterrupt, EOFError):
        raise SystemExit('Pembuatan akun dibatalkan.') from None


if __name__ == '__main__':
    main()
