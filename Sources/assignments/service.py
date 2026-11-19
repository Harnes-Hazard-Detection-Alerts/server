"""Assignment excavator<->operator (api-draft 4.5).

Siklus: aktif sampai di-unassign HSE (keputusan owner 6 Nov 2026);
1 excavator dan 1 operator masing-masing maksimal 1 assignment aktif;
unassign = soft-state (status 'ended') supaya riwayat bisa ditelusuri.
Identitas equipment = equipmentInstanceId stabil dari tracking (string),
BUKAN koordinat bbox (CONTEXT "Equipment Assignment").
"""
import psycopg

from ..infrastructure.database.connection import connect

ENDED_REASON = 'unassigned_by_hse'
REASSIGNED_REASON = 'reassigned_by_hse'


class AssignmentError(Exception):
    """Kegagalan aturan bisnis; `code` snake_case untuk API (api-draft 6)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _row(row, online_ids=()):
    item = dict(id=str(row[0]), equipmentInstanceId=row[1], operatorId=str(row[2]),
                status=row[3], assignedAtMs=row[4], endedAtMs=row[5], endedReason=row[6])
    if len(row) > 7:
        item.update(operatorName=row[7], sessionActive=row[8], shiftActive=row[9],
                    shiftStartedAtMs=row[10],
                    devicesOnline=(row[11] if str(row[2]) in set(online_ids) else 0))
    if len(row) > 12:
        item['equipmentName'] = row[12]
    return item


_SELECT = (
    'SELECT a.id, a.equipment_instance_id, a.operator_id, a.status, '
    'a.assigned_at_ms, a.ended_at_ms, a.ended_reason, u.display_name, '
    'EXISTS (SELECT 1 FROM sessions s WHERE s.user_id=a.operator_id '
    '        AND s.expires_at>now()), '
    'EXISTS (SELECT 1 FROM shift_bindings b WHERE b.operator_id=a.operator_id '
    '        AND b.equipment_instance_id=a.equipment_instance_id '
    '        AND b.operation_started_at_ms IS NOT NULL '
    '        AND EXISTS (SELECT 1 FROM sessions s WHERE s.user_id=b.operator_id '
    '                    AND s.expires_at>now())), '
    '(SELECT b.operation_started_at_ms FROM shift_bindings b '
    ' WHERE b.operator_id=a.operator_id '
    ' AND b.equipment_instance_id=a.equipment_instance_id), '
    '(SELECT count(*) FROM device_tokens d WHERE d.operator_id=a.operator_id), '
    'e.name '
    'FROM assignments a '
    'JOIN users u ON u.id=a.operator_id '
    'JOIN equipment_instances e '
    '  ON e.equipment_instance_id=a.equipment_instance_id '
)


def create_assignment(equipment_instance_id, operator_id, now_ms,
                      connection_factory=connect):
    """Assign one active operator to one equipment, atomically.

    Repeating the same assignment is idempotent. Changing an equipment's
    operator ends the previous assignment and clears its live shift binding.
    An operator assigned to another equipment must be unassigned first.
    """
    with connection_factory() as connection:
        equipment = connection.execute(
            'SELECT name FROM equipment_instances '
            'WHERE equipment_instance_id=%s FOR SHARE',
            (equipment_instance_id,)).fetchone()
        if equipment is None:
            raise AssignmentError('not_found')
        operator = connection.execute(
            'SELECT id, role, is_active, display_name FROM users WHERE id=%s FOR SHARE',
            (operator_id,)).fetchone()
        if operator is None:
            raise AssignmentError('not_found')
        if operator[1] != 'operator' or not operator[2]:
            raise AssignmentError('operator_inactive')
        existing = connection.execute(
            'SELECT id, equipment_instance_id, operator_id, status, assigned_at_ms, '
            'ended_at_ms, ended_reason FROM assignments '
            'WHERE equipment_instance_id=%s AND status=%s FOR UPDATE',
            (equipment_instance_id, 'active')).fetchone()
        if existing is not None and str(existing[2]) == str(operator_id):
            item = _row(existing)
            item.update(operatorName=operator[3], equipmentName=equipment[0], created=False)
            return item

        if existing is not None:
            running = connection.execute(
                'SELECT 1 FROM shift_bindings WHERE equipment_instance_id=%s '
                'AND operation_started_at_ms IS NOT NULL FOR UPDATE',
                (equipment_instance_id,)).fetchone()
            if running:
                raise AssignmentError('unit_operating')

        operator_assignment = connection.execute(
            'SELECT id FROM assignments WHERE operator_id=%s AND status=%s '
            'AND equipment_instance_id<>%s FOR UPDATE',
            (operator_id, 'active', equipment_instance_id)).fetchone()
        if operator_assignment is not None:
            raise AssignmentError('operator_already_assigned')

        if existing is not None:
            connection.execute(
                'UPDATE assignments SET status=%s, ended_at_ms=%s, ended_reason=%s '
                'WHERE id=%s', ('ended', now_ms, REASSIGNED_REASON, existing[0]))
            connection.execute(
                'DELETE FROM shift_bindings WHERE operator_id=%s '
                'AND equipment_instance_id=%s', (existing[2], equipment_instance_id))
        try:
            row = connection.execute(
                'INSERT INTO assignments(equipment_instance_id, operator_id, assigned_at_ms) '
                'VALUES (%s, %s, %s) RETURNING id, equipment_instance_id, operator_id, '
                'status, assigned_at_ms, ended_at_ms, ended_reason',
                (equipment_instance_id, operator_id, now_ms)).fetchone()
        except psycopg.errors.UniqueViolation as error:
            constraint = getattr(error.diag, 'constraint_name', '')
            code = ('operator_already_assigned'
                    if constraint == 'assignments_one_active_per_operator'
                    else 'equipment_already_assigned')
            raise AssignmentError(code) from None
        item = _row(row)
        item['operatorName'] = operator[3]
        item['equipmentName'] = equipment[0]
        item['created'] = True
        return item


def list_assignments(status=None, operator_id=None, equipment_instance_id=None,
                     online_ids=(), connection_factory=connect):
    """Daftar assignment + nama operator; urut terbaru dulu."""
    clauses, arguments = [], []
    if status is not None:
        clauses.append('a.status = %s'); arguments.append(status)
    if operator_id is not None:
        clauses.append('a.operator_id = %s'); arguments.append(operator_id)
    if equipment_instance_id is not None:
        clauses.append('a.equipment_instance_id = %s'); arguments.append(equipment_instance_id)
    where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
    with connection_factory() as connection:
        rows = connection.execute(
            _SELECT + where + ' ORDER BY a.assigned_at_ms DESC, a.id DESC',
            arguments).fetchall()
        return [_row(row, online_ids) for row in rows]


def list_active_for_operator(operator_id, online_ids=(), connection_factory=connect):
    items = list_assignments(status='active', operator_id=operator_id,
                             online_ids=online_ids,
                             connection_factory=connection_factory)
    for item in items:
        # Info kamera menyusul bersama modul cameras (api-draft 4.3) — jujur
        # tidak mengarang nama kamera dari tempat kosong.
        item.setdefault('cameraId', None)
    return items


def get_assignment(assignment_id, online_ids=(), connection_factory=connect):
    items = list_assignments(connection_factory=connection_factory,
                             online_ids=online_ids)
    return next((item for item in items if item['id'] == str(assignment_id)), None)


def end_assignment(assignment_id, now_ms, connection_factory=connect):
    """Unassign HSE: status 'ended'; raise not_found bila ID tak dikenal."""
    with connection_factory() as connection:
        running = connection.execute(
            'SELECT 1 FROM assignments a JOIN shift_bindings b '
            'ON b.operator_id=a.operator_id AND b.equipment_instance_id=a.equipment_instance_id '
            'WHERE a.id=%s AND a.status=%s AND b.operation_started_at_ms IS NOT NULL '
            'FOR UPDATE OF a, b', (assignment_id, 'active')).fetchone()
        if running:
            raise AssignmentError('unit_operating')
        row = connection.execute(
            'UPDATE assignments SET status=%s, ended_at_ms=%s, ended_reason=%s '
            'WHERE id=%s AND status=%s RETURNING id, equipment_instance_id, operator_id, '
            'status, assigned_at_ms, ended_at_ms, ended_reason',
            ('ended', now_ms, ENDED_REASON, assignment_id, 'active')).fetchone()
        if row is None:
            raise AssignmentError('not_found')
        connection.execute(
            'DELETE FROM shift_bindings WHERE operator_id=%s '
            'AND equipment_instance_id=%s', (row[2], row[1]))
        return _row(row)
