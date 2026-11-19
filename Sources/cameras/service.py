"""CRUD kamera (api-draft 4.3) — kamera = sumber frame yang diproses server.

Recording file didaftarkan via CLI lokal (`python -m Sources.cameras.cli add …`)
atau endpoint HSE; start/stop mengendalikan playback; snapshot mengambil frame
terbaru. Lifetime status dipelihara manager (playback.py) lewat `set_status`.
Frame & playback TIDAK disimpan di DB — hanya definisi kamera (P8: data ini
milik sisi deteksi server, bukan data interaksi app).
"""
import psycopg
from pathlib import Path

from ..infrastructure.database.connection import connect

_FIELDS = ('id, name, location, type, status, source, playback_fps, loop, '
           'last_seen_ms, health_note, registered_at_ms, desired_on')


class CameraError(Exception):
    """Kegagalan aturan bisnis; `code` snake_case utk API (api-draft 6 & 4.3)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _row(row):
    return dict(id=str(row[0]), name=row[1], location=row[2], type=row[3],
                status=row[4], source=row[5], playbackFps=row[6], loop=row[7],
                lastSeenMs=row[8], healthNote=row[9], registeredAtMs=row[10],
                desiredOn=row[11])


def create_camera(name, location, type_, source, playback_fps, loop, now_ms,
                  connection_factory=connect):
    """Daftarkan kamera. Validasi: source harus file yang ada (recording) atau
    URL rtsp:// (live CCTV); duplikat source → already_exists."""
    if type_ == 'recording' and not Path(source).is_file():
        raise CameraError('file_not_found')
    if type_ == 'rtsp' and not source.startswith('rtsp://'):
        raise CameraError('invalid_source')
    try:
        with connection_factory() as connection:
            row = connection.execute(
                'INSERT INTO cameras(name, location, type, source, playback_fps, '
                'loop, registered_at_ms) VALUES (%s, %s, %s, %s, %s, %s, %s) '
                'RETURNING ' + _FIELDS,
                (name, location, type_, source, playback_fps, loop, now_ms)).fetchone()
            return _row(row)
    except psycopg.errors.UniqueViolation:
        raise CameraError('already_exists') from None


def list_cameras(connection_factory=connect):
    with connection_factory() as connection:
        rows = connection.execute(
            'SELECT ' + _FIELDS + ' FROM cameras ORDER BY name, id').fetchall()
        return [_row(row) for row in rows]


def get_camera(camera_id, connection_factory=connect):
    with connection_factory() as connection:
        row = connection.execute('SELECT ' + _FIELDS + ' FROM cameras WHERE id=%s',
                                 (camera_id,)).fetchone()
        return _row(row) if row else None


def update_camera(camera_id, name=None, location=None, connection_factory=connect):
    """Ganti nama/lokasi kamera (PATCH /cameras/{id}, api-draft 4.3).
    Field None tidak diubah; return None bila kamera tak dikenal (handler 404)."""
    with connection_factory() as connection:
        row = connection.execute(
            'UPDATE cameras SET name=COALESCE(%s, name), location=COALESCE(%s, location) '
            'WHERE id=%s RETURNING ' + _FIELDS,
            (name, location, camera_id)).fetchone()
        return _row(row) if row else None


def camera_for_equipment(equipment_instance_id, connection_factory=connect):
    """Kamera milik equipment ber-ID fallback '{8 char id kamera}-eq-N'
    (core/evaluator.py). None bila pola tak cocok atau kamera tak ada."""
    prefix, separator, _ = str(equipment_instance_id).partition('-eq-')
    if not separator or len(prefix) != 8:
        return None
    with connection_factory() as connection:
        row = connection.execute(
            'SELECT ' + _FIELDS + ' FROM cameras WHERE id::text LIKE %s ORDER BY id LIMIT 1',
            (prefix + '%',)).fetchone()
        return _row(row) if row else None


def delete_camera(camera_id, connection_factory=connect):
    """Hapus definisi kamera; return None bila tak dikenal (handler 404).

    Kamera yang masih dirujuk unit (equipment_cameras) atau alert tidak bisa
    dihapus: FK-nya RESTRICT. psycopg3 melaporkan itu sebagai
    `RestrictViolation` — BUKAN subclass `ForeignKeyViolation` — jadi keduanya
    harus ditangkap; kalau tidak, error jatuh ke handler umum dan jadi 503
    `database_unavailable` (padahal masalahnya relasi, bukan database).
    """
    try:
        with connection_factory() as connection:
            row = connection.execute('DELETE FROM cameras WHERE id=%s RETURNING id',
                                     (camera_id,)).fetchone()
            return str(row[0]) if row else None
    except (psycopg.errors.ForeignKeyViolation,
            psycopg.errors.RestrictViolation):
        raise CameraError('camera_has_alerts') from None


def set_status(camera_id, status, now_ms, health_note=None, connection_factory=connect):
    """Transisi status oleh manager: online/offline/error (+ lastSeenMs, healthNote)."""
    with connection_factory() as connection:
        connection.execute(
            'UPDATE cameras SET status=%s, last_seen_ms=%s, health_note=%s WHERE id=%s',
            (status, now_ms, health_note, camera_id))


def set_desired(camera_id, desired, connection_factory=connect):
    """Intent supervisor (017): start/stop HSE menulis desired_on; supervisor
    CameraManager (playback.py) membuat runtime mengejar intent — restart
    server tidak pernah meninggalkan "online bohong" atau kamera terlupakan."""
    with connection_factory() as connection:
        connection.execute('UPDATE cameras SET desired_on=%s WHERE id=%s',
                          (bool(desired), camera_id))
