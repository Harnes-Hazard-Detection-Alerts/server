"""Perangkat operator (api-draft 4.6).

iPhone = titik kirim alert + alamat routing; metadata Watch opsional
(CONTEXT "Device Mapping": Watch tidak pernah bicara langsung ke server).
Presence (status online) ditentukan koneksi SSE — pengisiannya menyusul
bersama routing SSE operator (M6); hari ini default 'offline'.
"""
from ..infrastructure.database.connection import connect


class DeviceError(Exception):
    """Kegagalan aturan bisnis; `code` snake_case untuk API (api-draft 6)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


_FIELDS = ('id, operator_id, model, os_version, app_version, watch_present, '
           'watch_model, watch_os_version, status, last_seen_ms, registered_at_ms')


def _row(row):
    return dict(id=str(row[0]), operatorId=str(row[1]), model=row[2], osVersion=row[3],
               appVersion=row[4],
               watch=dict(present=row[5], model=row[6], osVersion=row[7]) if row[5]
               else dict(present=False),
               status=row[8], lastSeenMs=row[9], registeredAtMs=row[10])


def register_device(operator_id, model, os_version, app_version, watch,
                    now_ms, connection_factory=connect):
    """Registrasi/insert perangkat operator; return device (status offline).

    Defense-in-depth (sejajar create_assignment): target operator harus
    ada, berperan operator, dan aktif."""
    watch_present_value = watch.get('present') if isinstance(watch, dict) else None
    watch_present = bool(watch_present_value is True)
    watch_model = watch.get('model') if watch_present else None
    watch_os = watch.get('osVersion') if watch_present else None
    if watch_present and (not watch_model or not watch_os):
        raise DeviceError('invalid_watch_metadata')
    with connection_factory() as connection:
        operator = connection.execute(
            'SELECT id, role, is_active FROM users WHERE id=%s', (operator_id,)).fetchone()
        if operator is None:
            raise DeviceError('not_found')
        if operator[1] != 'operator' or not operator[2]:
            raise DeviceError('operator_inactive')
        row = connection.execute(
            'INSERT INTO devices(operator_id, model, os_version, app_version, '
            'watch_present, watch_model, watch_os_version, registered_at_ms) '
            f'VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_FIELDS}',
            (operator_id, model, os_version, app_version,
             watch_present, watch_model, watch_os, now_ms)).fetchone()
        return _row(row)


def list_devices(operator_id=None, connection_factory=connect, online_ids=()):
    """Daftar perangkat (operator tertentu bila diberi); urut terdaftar terbaru.

    status 'online' = operator punya stream SSE hidup (presence bus)."""
    online = {str(identifier) for identifier in online_ids}
    where = 'WHERE operator_id = %s' if operator_id is not None else ''
    arguments = (operator_id,) if operator_id is not None else ()
    with connection_factory() as connection:
        rows = connection.execute(
            f'SELECT {_FIELDS} FROM devices {where} '
            'ORDER BY registered_at_ms DESC, id DESC', arguments).fetchall()
        items = []
        for row in rows:
            item = _row(row)
            if item['operatorId'] in online:
                item['status'] = 'online'
                # lastSeenMs & event device.status menyusul bersama typed event;
                # hari ini presence = operator punya stream hidup (bus).
            items.append(item)
        return items


def unregister_device(device_id, operator_id=None, connection_factory=connect):
    """Cabut pendaftaran. operator_id diberi saat operator mencabut miliknya
    (cek kepemilikan -> device_not_owned); HSE boleh tanpa filter."""
    clauses, arguments = ['id = %s'], [device_id]
    if operator_id is not None:
        clauses.append('operator_id = %s'); arguments.append(operator_id)
    with connection_factory() as connection:
        row = connection.execute(
            f'DELETE FROM devices WHERE ' + ' AND '.join(clauses) +
            f' RETURNING {_FIELDS}', arguments).fetchone()
        if row is None:
            # Bedakan: tak dikenal vs dikenal tapi bukan milik operator.
            known = connection.execute('SELECT 1 FROM devices WHERE id = %s',
                                       (device_id,)).fetchone()
            if known is None:
                raise DeviceError('not_found')
            raise DeviceError('device_not_owned')
        return _row(row)
