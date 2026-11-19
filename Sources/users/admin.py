"""Manajemen akun oleh HSE (api-draft 4.2 N2-B): buat, ganti password, ubah profil.

Kredensial tetap milik server: scrypt via auth.passwords, sesi 12 jam, rate-limit
login — semua aturan itu berjalan di modul auth dan tidak diubah di sini. Modul ini
hanya operasi baris users + efek sampingnya:

- create_account: akun baru; UniqueViolation username dibiarkan mentah —
  pemanggil HTTP memetakannya ke 409 already_exists (CLI memetakannya ke pesan).
- set_password: ganti hash + CABUT semua sesi target (keputusan 9 Nov 2026 —
  reset password = peristiwa keamanan; sesi lama tidak boleh selamat sampai
  kedaluwarsa 12 jamnya).
- update_account: displayName/active. active=false = soft-disable: is_active
  memblokir otentikasi seketika (AuthService sudah cek kolom ini), lalu
  assignment aktif diakhiri dan device_tokens + shift_bindings target dibersihkan
  di transaksi yang sama agar akses unit serta push APNs berhenti. Guard
  anti-lockout "jangan nonaktifkan akun sendiri" ada di lapisan handler (butuh
  identitas pemanggil), bukan di sini.
"""
import re
import time
from uuid import UUID

from ..auth.passwords import hash_password
from ..infrastructure.database.connection import connect

# Sama dengan aturan CLI (users/create.py): 1–100 karakter, diawali huruf/angka.
USERNAME_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')

_COLUMNS = 'id, username, display_name, role, is_active'


class UserError(Exception):
    """Kode domain ('not_found') — dipetakan handler ke status HTTP."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _user(row):
    return dict(id=str(row[0]), username=row[1], displayName=row[2],
                role=row[3], active=row[4])


def validate_account_fields(username, display_name, role):
    """Validasi bentuk; password divalidasi hash_password (12–256)."""
    if not USERNAME_PATTERN.fullmatch(username or ''):
        raise ValueError('Username: 1–100 karakter huruf/angka, titik, garis bawah, '
                         'atau tanda minus; diawali huruf/angka.')
    if not 1 <= len(display_name.strip()) <= 200 or role not in ('operator', 'hse'):
        raise ValueError('Nama atau role tidak valid.')


def create_account(username, display_name, role, password, connection_factory=connect):
    validate_account_fields(username, display_name, role)
    encoded = hash_password(password)  # juga memvalidasi panjang 12–256
    with connection_factory() as connection:
        row = connection.execute(
            f'INSERT INTO users(username, display_name, role, password_hash) '
            f'VALUES (%s, %s, %s, %s) RETURNING {_COLUMNS}',
            (username, display_name.strip(), role, encoded)).fetchone()
        return _user(row)


def set_password(user_id, password, connection_factory=connect):
    encoded = hash_password(password)
    with connection_factory() as connection:
        running = connection.execute(
            'SELECT 1 FROM shift_bindings WHERE operator_id=%s '
            'AND operation_started_at_ms IS NOT NULL', (UUID(user_id),)).fetchone()
        if running:
            raise UserError('unit_operating')
        row = connection.execute(
            f'UPDATE users SET password_hash=%s WHERE id=%s RETURNING {_COLUMNS}',
            (encoded, UUID(user_id))).fetchone()
        if row is None:
            raise UserError('not_found')
        connection.execute('DELETE FROM sessions WHERE user_id=%s', (UUID(user_id),))
        return _user(row)


def update_account(user_id, display_name=None, active=None, connection_factory=connect):
    """PATCH displayName/active (api-draft 4.2). active=false → soft-disable."""
    updates, params = [], []
    if display_name is not None:
        updates.append('display_name=%s')
        params.append(display_name.strip())
    if active is not None:
        updates.append('is_active=%s')
        params.append(active)
    if not updates:
        raise ValueError('PATCH kosong: displayName atau active wajib ada.')
    with connection_factory() as connection:
        if active is False:
            running = connection.execute(
                'SELECT 1 FROM shift_bindings WHERE operator_id=%s '
                'AND operation_started_at_ms IS NOT NULL', (UUID(user_id),)).fetchone()
            if running:
                raise UserError('unit_operating')
        row = connection.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE id=%s RETURNING {_COLUMNS}",
            (*params, UUID(user_id))).fetchone()
        if row is None:
            raise UserError('not_found')
        if active is False:
            connection.execute(
                'UPDATE assignments SET status=%s, ended_at_ms=%s, ended_reason=%s '
                'WHERE operator_id=%s AND status=%s',
                ('ended', time.time_ns() // 1_000_000, 'operator_deactivated',
                 UUID(user_id), 'active'))
            connection.execute('DELETE FROM device_tokens WHERE operator_id=%s',
                               (UUID(user_id),))
            connection.execute('DELETE FROM shift_bindings WHERE operator_id=%s',
                               (UUID(user_id),))
        return _user(row)
