"""Server-side opaque sessions; role always comes from the current user row."""
"""Sessions & policies auth: token 43-karakter (sha256 disimpan), scrypt password.

Kebijakan DEMO (11 Nov 2026, keputusan owner):
- Sesi default 365 hari (HARNES_SESSION_DAYS) — praktis tak kedaluarsa.
- Login operator baru TIDAK memutus sesi device lain (dulu exclusive);
  hidupkan lagi produksi via HARNES_OPERATOR_SESSIONS_EXCLUSIVE=1.
- Rate-limit login default MATI (handler); nyalakan via HARNES_LOGIN_LIMIT=1.
"""
import hashlib
import os
import secrets

from ..infrastructure.database.connection import connect
from .passwords import verify_password

# Valid-format dummy hash keeps unknown users on the same password-work path.
DUMMY_HASH = 'scrypt-v1$' + '00' * 16 + '$' + '00' * 32


class Unauthorized(Exception):
    pass


class AuthConflict(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def profile(row):
    return dict(id=str(row[0]), username=row[1], display_name=row[2], role=row[3])


class AuthService:
    def __init__(self, connection_factory=connect):
        self.connect = connection_factory

    def login(self, username, password):
        with self.connect() as connection:
            row = connection.execute('SELECT id, username, display_name, role, password_hash, is_active FROM users WHERE lower(username)=lower(%s)', (username,)).fetchone()
            valid = verify_password(password, row[4] if row else DUMMY_HASH)
            if not valid or not row or not row[5]:
                raise Unauthorized()
            # Recheck activity/hash while holding a row lock before issuing the session.
            current = connection.execute('SELECT id, username, display_name, role, password_hash, is_active FROM users WHERE id=%s FOR SHARE', (row[0],)).fetchone()
            if not current or not current[5] or current[4] != row[4]:
                raise Unauthorized()
            token = secrets.token_urlsafe(32)
            # Demo permits multiple device sessions by default. When the
            # exclusive production policy is enabled, guard an active
            # operation before clearing its binding and session.
            if (current[3] == 'operator'
                    and os.environ.get('HARNES_OPERATOR_SESSIONS_EXCLUSIVE') == '1'):
                running = connection.execute(
                    'SELECT 1 FROM shift_bindings b JOIN sessions s ON s.user_id=b.operator_id '
                    'WHERE b.operator_id=%s AND b.operation_started_at_ms IS NOT NULL '
                    'AND s.expires_at>now() LIMIT 1 FOR UPDATE OF b', (row[0],)).fetchone()
                if running:
                    raise AuthConflict('unit_operating')
                connection.execute('DELETE FROM shift_bindings WHERE operator_id=%s',
                                   (row[0],))
                connection.execute('DELETE FROM sessions WHERE user_id=%s', (row[0],))
            connection.execute(
                'DELETE FROM sessions WHERE user_id=%s AND expires_at <= now()',
                (row[0],))
            session_days = float(os.environ.get('HARNES_SESSION_DAYS') or '365')
            expires = connection.execute(
                "INSERT INTO sessions(token_hash,user_id,expires_at) "
                "VALUES (%s,%s,now() + %s * interval '1 day') RETURNING expires_at",
                (token_hash(token), row[0], session_days)).fetchone()[0]
            return dict(access_token=token, token_type='Bearer', expires_at=expires.isoformat(), user=profile(current))

    def authenticate(self, token):
        if not isinstance(token, str) or len(token) != 43:
            raise Unauthorized()
        with self.connect() as connection:
            row = connection.execute('''SELECT u.id,u.username,u.display_name,u.role
                FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=%s AND s.expires_at>now() AND u.is_active''', (token_hash(token),)).fetchone()
            if not row:
                raise Unauthorized()
            return profile(row)

    def logout(self, token):
        user = self.authenticate(token)
        with self.connect() as connection:
            connection.execute('DELETE FROM sessions WHERE token_hash=%s', (token_hash(token),))
            connection.execute(
                'DELETE FROM shift_bindings WHERE operator_id=%s AND NOT EXISTS '
                '(SELECT 1 FROM sessions WHERE user_id=%s AND expires_at>now())',
                (user['id'], user['id']))
