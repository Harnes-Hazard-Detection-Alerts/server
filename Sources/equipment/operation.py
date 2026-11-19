"""Cached operation state derived from an authenticated operator binding."""
import threading
import time

from ..infrastructure.database.connection import connect


class OperationError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class OperationStatus:
    def __init__(self, connection_factory=connect):
        self._connect = connection_factory
        self._lock = threading.Lock()
        self._active = {}  # equipment_instance_id -> session expiry epoch-ms
        self._started = {}

    def reload(self):
        """Gerbang operasional unit = assignment aktif + sesi operator valid.

        Kebijakan 12 Nov 2026 (akurasi alert): perlindungan menyala selama
        excavator punya operator yang DITUGASKAN dan sesi login-nya hidup —
        bukan hanya baris shift_bindings (binding bisa hilang saat assignment
        dipindahkan — kasus dewi→Timur 11 Nov — dan pindah assignment via
        API tidak pernah membuat binding baru). shift_bindings tetap
        dihormati sebagai tombol "Stop" eksplisit: binding dengan
        operation_started NULL = operator menekan berhenti → unit nonaktif
        walau sesi masih hidup.
        """
        now_ms = time.time_ns() // 1_000_000
        with self._connect() as connection:
            assignments = connection.execute(
                'SELECT a.equipment_instance_id, '
                '(extract(epoch FROM max(s.expires_at)) * 1000)::bigint '
                'FROM assignments a '
                'LEFT JOIN sessions s ON s.user_id=a.operator_id AND s.expires_at>now() '
                "WHERE a.status='active' GROUP BY a.equipment_instance_id"
            ).fetchall()
            bindings = connection.execute(
                'SELECT equipment_instance_id, max(operation_started_at_ms) '
                'FROM shift_bindings GROUP BY equipment_instance_id'
            ).fetchall()
        stopped = {row[0] for row in bindings if row[1] is None}
        started = {row[0]: int(row[1]) for row in bindings if row[1] is not None}
        active = {}
        for equipment_instance_id, expires_ms in assignments:
            if expires_ms is None or int(expires_ms) <= now_ms:
                continue               # operator tak punya sesi hidup
            if equipment_instance_id in stopped:
                continue               # operator menekan Stop (binding eksplisit NULL)
            active[equipment_instance_id] = int(expires_ms)
        with self._lock:
            self._active = active
            self._started = {equipment_id: value for equipment_id, value in started.items()
                             if equipment_id in active}

    def is_active(self, equipment_instance_id):
        now_ms = time.time_ns() // 1_000_000
        with self._lock:
            expires = self._active.get(equipment_instance_id)
            if expires is None:
                return False
            if expires <= now_ms:
                self._active.pop(equipment_instance_id, None)
                return False
            return True

    def set_cached(self, equipment_instance_id, active, expires_at_ms=None,
                   started_at_ms=None):
        with self._lock:
            if active and expires_at_ms:
                self._active[equipment_instance_id] = int(expires_at_ms)
                if started_at_ms is not None:
                    self._started[equipment_instance_id] = int(started_at_ms)
            else:
                self._active.pop(equipment_instance_id, None)
                self._started.pop(equipment_instance_id, None)

    def started_at(self, equipment_instance_id):
        with self._lock:
            return self._started.get(equipment_instance_id)

    def active_ids(self):
        """Snapshot equipment_instance_id yang sedang operating — dipakai gate
        model per kamera (playback menentukan kamera mana yang perlu detector)."""
        with self._lock:
            return set(self._active)


def set_operation_active(operator_id, equipment_instance_id, active, now_ms,
                         connection_factory=connect):
    """Toggle operation only for the operator's currently assigned login binding."""
    with connection_factory() as connection:
        row = connection.execute(
            'SELECT b.operation_started_at_ms, s.expires_at '
            'FROM shift_bindings b '
            'JOIN assignments a ON a.equipment_instance_id=b.equipment_instance_id '
            ' AND a.operator_id=b.operator_id AND a.status=%s '
            'JOIN sessions s ON s.user_id=b.operator_id AND s.expires_at>now() '
            'WHERE b.operator_id=%s AND b.equipment_instance_id=%s FOR UPDATE OF b',
            ('active', operator_id, equipment_instance_id)).fetchone()
        if row is None:
            raise OperationError('assignment_mismatch')
        if active:
            # Retry Start pada sesi yang sama bersifat idempoten. Reset workflow
            # hanya ketika binding sebelumnya benar-benar sudah berhenti.
            if row[0] is None:
                connection.execute(
                    'INSERT INTO alert_workflow_history(alert_id, restarted_at_ms, snapshot) '
                    'SELECT a.id, %s, to_jsonb(a) FROM alerts a '
                    'WHERE equipment_instance_id=%s AND status=%s AND kind IN (%s, %s)',
                    (now_ms, equipment_instance_id, 'active',
                     'person_entered_zone', 'person_high_risk'))
                connection.execute(
                    'UPDATE alerts SET operator_response=NULL, operator_responded_at_ms=NULL, '
                    'operator_responded_by=NULL, hazard_active_10s_at_ms=NULL, '
                    'hse_on_my_way_at_ms=NULL, hse_on_my_way_by=NULL, hse_on_my_way_name=NULL, '
                    'hse_confirmed_safe_at_ms=NULL, hse_confirmed_safe_by=NULL, '
                    'hse_confirmed_safe_name=NULL WHERE equipment_instance_id=%s '
                    'AND status=%s AND kind IN (%s, %s)',
                    (equipment_instance_id, 'active',
                     'person_entered_zone', 'person_high_risk'))
                connection.execute(
                    'UPDATE shift_bindings SET operation_started_at_ms=%s '
                    'WHERE operator_id=%s AND equipment_instance_id=%s',
                    (now_ms, operator_id, equipment_instance_id))
        else:
            unvalidated_hazard = connection.execute(
                'SELECT 1 FROM alerts a JOIN shift_bindings b '
                'ON b.equipment_instance_id=a.equipment_instance_id '
                'WHERE a.equipment_instance_id=%s AND a.status=%s '
                'AND a.kind IN (%s, %s) AND '
                '(b.operation_started_at_ms IS NULL OR a.hse_confirmed_safe_at_ms IS NULL '
                'OR a.hse_confirmed_safe_at_ms < b.operation_started_at_ms) LIMIT 1',
                (equipment_instance_id, 'active', 'person_entered_zone',
                 'person_high_risk')).fetchone()
            if unvalidated_hazard:
                raise OperationError('active_hazard')
            connection.execute(
                'UPDATE shift_bindings SET operation_started_at_ms=NULL '
                'WHERE operator_id=%s AND equipment_instance_id=%s',
                (operator_id, equipment_instance_id))
        expiry_ms = int(row[1].timestamp() * 1000)
    return expiry_ms
