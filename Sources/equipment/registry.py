"""Server-owned one-camera/one-equipment registry for the MVP."""
import threading
import time

import psycopg

from ..infrastructure.database.connection import connect


class CameraIdentityCatalog:
    """Peta kamera→unit ter-DAFTAR: seam identitas evaluator (14 Nov 2026).

    Kasus lapangan: script produser RTSP me-relink unit seed ke kamera baru,
    sehingga identitas fallback evaluator ("prefix kamera -eq-1") tidak lagi
    sama dengan equipmentInstanceId assignment/operating/radius di app. Katalog
    ini dipasang composition root (`identity_for`) supaya alert/status/radius
    memakai ID unit ter-DAFTAR. Fail-open: kamera tak ter-link atau DB gagal
    dibaca -> None (evaluator kembali ke fallback prefix; deteksi tetap jalan).
    TTL cache ringan — relink/pendaftaran unit terlihat maksimal ttl_s kemudian.
    """

    def __init__(self, connection_factory=connect, ttl_s=5.0):
        self._connect = connection_factory
        self.ttl_s = max(0.0, float(ttl_s))
        self._lock = threading.Lock()
        self._map = {}
        self._loaded_at = float('-inf')

    def equipment_for(self, camera_id):
        """equipmentInstanceId ter-link registry untuk kamera ini, atau None."""
        now = time.monotonic()
        with self._lock:
            if now - self._loaded_at < self.ttl_s:
                return self._map.get(str(camera_id))
            try:
                with self._connect() as connection:
                    rows = connection.execute(
                        'SELECT camera_id, equipment_instance_id '
                        'FROM equipment_cameras').fetchall()
                self._map = {str(camera_id): equipment
                             for camera_id, equipment in rows}
            except Exception as error:
                print(f'[equipment] peta identitas kamera gagal dibaca '
                      f'({type(error).__name__}) — evaluator fallback prefix',
                      flush=True)
            self._loaded_at = now
            return self._map.get(str(camera_id))


class EquipmentError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def list_equipment(connection_factory=connect):
    with connection_factory() as connection:
        rows = connection.execute(
            'SELECT ec.equipment_instance_id, ec.camera_id, c.name, c.status, '
            'c.location, l.display_name, l.cabin_phone '
            'FROM equipment_cameras ec JOIN cameras c ON c.id=ec.camera_id '
            'LEFT JOIN equipment_labels l USING (equipment_instance_id) '
            'ORDER BY c.name, ec.equipment_instance_id').fetchall()
    return [dict(equipmentInstanceId=row[0], cameraId=str(row[1]),
                 cameraName=row[2], cameraStatus=row[3], location=row[4],
                 name=row[5] or row[2], cabinPhone=row[6]) for row in rows]


def available_cameras(connection_factory=connect):
    """Kamera bebas yang bisa dijadikan unit oleh HSE — recording maupun
    RTSP (keduanya sumber frame yang diproses server; keputusan 13 Nov 2026)."""
    with connection_factory() as connection:
        rows = connection.execute(
            'SELECT c.id, c.name, c.location, c.type, c.status, c.source, '
            'c.playback_fps, c.loop, c.last_seen_ms, c.health_note, c.registered_at_ms, '
            'c.desired_on '
            'FROM cameras c LEFT JOIN equipment_cameras ec ON ec.camera_id=c.id '
            "WHERE ec.camera_id IS NULL AND c.type IN ('recording', 'rtsp') "
            'ORDER BY c.name, c.id'
        ).fetchall()
    from ..cameras.service import _row
    return [_row(row) for row in rows]


def create_from_camera(camera_id, name, user_id, now_ms, connection_factory=connect):
    """Link kamera yang belum terpakai (recording/rtsp) sebagai unit,
    mempertahankan skema ID fallback yang sudah ada."""
    clean_name = (name or '').strip()
    if not 1 <= len(clean_name) <= 100:
        raise ValueError('invalid_name')
    equipment_id = f'{str(camera_id)[:8]}-eq-1'
    try:
        with connection_factory() as connection:
            # Serialize registrations so two HSE requests cannot both accept
            # the same name before either new camera link is committed.
            connection.execute('SELECT pg_advisory_xact_lock(681204713)')
            camera = connection.execute(
                "SELECT id FROM cameras WHERE id=%s "
                "AND type IN ('recording', 'rtsp') FOR SHARE",
                (camera_id,)).fetchone()
            if camera is None:
                raise EquipmentError('not_found')
            names = connection.execute(
                'SELECT COALESCE(NULLIF(l.display_name, \'\'), c.name) '
                'FROM equipment_cameras ec JOIN cameras c ON c.id=ec.camera_id '
                'LEFT JOIN equipment_labels l USING (equipment_instance_id)').fetchall()
            if any(row[0].strip().lower() == clean_name.lower() for row in names):
                raise EquipmentError('equipment_name_in_use')
            # Assignment and shift bindings reference this catalogue. A newly
            # linked camera must be assignable before its first detection.
            connection.execute(
                'INSERT INTO equipment_instances(equipment_instance_id, name) '
                'VALUES (%s, %s) ON CONFLICT (equipment_instance_id) DO NOTHING',
                (equipment_id, clean_name))
            connection.execute(
                'INSERT INTO equipment_cameras(equipment_instance_id, camera_id, '
                'created_at_ms, created_by) VALUES (%s, %s, %s, %s)',
                (equipment_id, camera_id, now_ms, user_id))
            connection.execute(
                'INSERT INTO equipment_labels(equipment_instance_id, display_name, '
                'cabin_phone, updated_at_ms, updated_by) VALUES (%s, %s, NULL, %s, %s) '
                'ON CONFLICT (equipment_instance_id) DO UPDATE SET '
                'display_name=EXCLUDED.display_name, updated_at_ms=EXCLUDED.updated_at_ms, '
                'updated_by=EXCLUDED.updated_by',
                (equipment_id, clean_name, now_ms, user_id))
            # Jembatan katalog (keputusan owner 13 Nov 2026): migrasi 013/014
            # memasang FK assignments/shift_bindings → equipment_instances,
            # sedangkan registrasi unit lewat app hanya menulis registry di
            # atas. Tanpa baris katalog, alur REAL (unit dibuat HSE → assign)
            # ditolak not_found. Registry tetap satu sumber kebenaran; baris
            # katalog hanya salinan identitas+nama yang ikut disinkronkan.
            connection.execute(
                'INSERT INTO equipment_instances(equipment_instance_id, name) '
                'VALUES (%s, %s) ON CONFLICT (equipment_instance_id) '
                'DO UPDATE SET name=EXCLUDED.name',
                (equipment_id, clean_name))
    except psycopg.errors.UniqueViolation:
        raise EquipmentError('camera_already_assigned') from None
    return next((item for item in list_equipment(connection_factory)
                 if item['equipmentInstanceId'] == equipment_id), None)


def camera_for_equipment(equipment_instance_id, connection_factory=connect):
    with connection_factory() as connection:
        row = connection.execute(
            'SELECT c.id, c.name, c.location, c.type, c.status, c.source, '
            'c.playback_fps, c.loop, c.last_seen_ms, c.health_note, c.registered_at_ms, '
            'c.desired_on '
            'FROM equipment_cameras ec JOIN cameras c ON c.id=ec.camera_id '
            'WHERE ec.equipment_instance_id=%s', (equipment_instance_id,)).fetchone()
    if row is None:
        # Compatibility for equipment IDs created before migration 016 or
        # external fixtures not represented in the local camera registry.
        from ..cameras.service import camera_for_equipment as legacy_lookup
        return legacy_lookup(equipment_instance_id, connection_factory)
    from ..cameras.service import _row
    return _row(row)


def delete_equipment(equipment_instance_id, connection_factory=connect):
    """Unlink a non-operating unit and release its camera; history is retained."""
    with connection_factory() as connection:
        active = connection.execute(
            'SELECT 1 FROM assignments WHERE equipment_instance_id=%s AND status=%s',
            (equipment_instance_id, 'active')).fetchone()
        if active:
            raise EquipmentError('active_assignment')
        row = connection.execute(
            'DELETE FROM equipment_cameras WHERE equipment_instance_id=%s '
            'RETURNING camera_id', (equipment_instance_id,)).fetchone()
        if row is None:
            raise EquipmentError('not_found')
        connection.execute('DELETE FROM equipment_labels WHERE equipment_instance_id=%s',
                           (equipment_instance_id,))
        connection.execute('DELETE FROM equipment_zone_settings WHERE equipment_instance_id=%s',
                           (equipment_instance_id,))
        return str(row[0])
