"""Direktori operator untuk HSE (api-draft 4.2).

HSE butuh daftar operator; server satu-satunya sumber kebenaran akun
(tidak ada signup publik). Bentuk objek user dipakai bersama oleh
GET /users dan respons POST/PATCH manajemen akun (users/admin.py).

devicesOnline = jumlah perangkat terdaftar operator (device_tokens,
jalur /me/device) HANYA jika operator punya stream hidup (presence
SSE/WS dari bus; api-draft 4.8) — semua perangkat operator yang punya
stream hidup dianggap online sampai iPhone melaporkan baris 'watch'
per perangkat. Tabel devices lama (M5, diparkir) tidak lagi dihitung.
"""
from uuid import UUID

from ..infrastructure.database.connection import connect

_TABLE = ('FROM users u '
          'LEFT JOIN (SELECT operator_id, count(*) AS active_count '
          '           FROM assignments WHERE status = %s GROUP BY operator_id) a '
          '  ON a.operator_id = u.id ')
_COLUMNS = ('u.id, u.username, u.display_name, u.role, u.is_active, '
            'COALESCE(a.active_count, 0) ')


def _item(row, device_count, online):
    return dict(id=str(row[0]), username=row[1], displayName=row[2], role=row[3],
                active=row[4], activeAssignments=row[5],
                devicesOnline=(device_count if str(row[0]) in online else 0))


def list_users(role=None, connection_factory=connect, online_ids=()):
    """Daftar akun + hitungan assignment aktif per operator (tabel assignments
    diparkir — nilainya beku 0 di database baru; dibiarkan demi bentuk kontrak)."""
    online = {str(identifier) for identifier in online_ids}
    with connection_factory() as connection:
        rows = connection.execute(
            'SELECT ' + _COLUMNS + _TABLE +
            ('WHERE u.role = %s ' if role is not None else '') + 'ORDER BY u.username',
            (('active',) + ((role,) if role is not None else ()))).fetchall()
        device_counts = dict(connection.execute(
            'SELECT operator_id, count(*) FROM device_tokens GROUP BY operator_id').fetchall())
        return [_item(row, device_counts.get(row[0], 0), online) for row in rows]


def get_item(user_id, online_ids=(), connection_factory=connect):
    """Satu objek user — bentuk sama dengan item GET /users; None jika tak ada."""
    online = {str(identifier) for identifier in online_ids}
    with connection_factory() as connection:
        row = connection.execute(
            'SELECT ' + _COLUMNS + _TABLE + 'WHERE u.id=%s',
            ('active', UUID(user_id))).fetchone()
        if row is None:
            return None
        device_count = connection.execute(
            'SELECT count(*) FROM device_tokens WHERE operator_id=%s', (row[0],)).fetchone()[0]
        return _item(row, device_count, online)
