"""Penyimpanan riwayat alert (api-draft 4.7 — P6 menutup celah SSE).

Lifecycle: person_entered_zone/person_high_risk dicatat `active`; transisi ke
high-risk menutup alert entered sebelumnya, dan zone_cleared menutup alert
aktif untuk pasangan (camera, equipment) yang sama -> `resolved` (keputusan
"bahaya selesai" dari evaluasi risiko server — CONTEXT "Arti status", bukan
tindakan pengguna). person_count_changed dicatat `resolved` langsung
(kejadian sesaat, bukan status bahaya).

Fail-open: pipeline TIDAK boleh mati karena riwayat gagal ditulis —
pemanggil menangkap error penyimpanan (server mencatat peringatan lalu
tetap publish ke SSE).
"""
import time

from ..core.operator_response import OPERATOR_RESPONSES, OperatorResponseError
from ..infrastructure.database.connection import connect

_COLUMNS = ('id', 'kind', 'message', 'camera_id', 'equipment_instance_id',
            'persons_in_zone', 'occurred_at_ms', 'status', 'resolved_at_ms',
            'created_at_ms', 'operator_response', 'operator_responded_at_ms',
            'operator_responded_by', 'hazard_active_10s_at_ms',
            'hse_on_my_way_at_ms', 'hse_on_my_way_by', 'hse_on_my_way_name',
            'hse_confirmed_safe_at_ms', 'hse_confirmed_safe_by',
            'hse_confirmed_safe_name')
_FIELDS = ', '.join(_COLUMNS)
_A_FIELDS = ', '.join('a.' + column for column in _COLUMNS)
_DANGER_KINDS = ('person_entered_zone', 'person_high_risk')


def _deliveries_of(connection, alert_id):
    rows = connection.execute(
        'SELECT id, device_id, push_device_id, operator_id, target, status, '
        'at_ms, channel, push_device_ref '
        'FROM alert_deliveries WHERE alert_id=%s ORDER BY at_ms', (alert_id,)).fetchall()
    return [dict(id=str(d[0]), deviceId=str(d[1]) if d[1] else None,
                 pushDeviceId=str(d[2]) if d[2] else None,
                 operatorId=str(d[3]) if d[3] else None,
                 target=d[4], status=d[5], atMs=d[6], channel=d[7],
                 pushDeviceRef=d[8]) for d in rows]


def _with_deliveries(connection, row):
    item = _row(row)
    item['deliveries'] = _deliveries_of(connection, row[0])
    if row[12]:
        actor = connection.execute('SELECT display_name FROM users WHERE id=%s',
                                   (row[12],)).fetchone()
        item['operatorName'] = actor[0] if actor else None
    return item


class AlertStoreError(Exception):
    """Error penyimpanan alert (dibalut caller jadi warning, bukan crash)."""


class AlertStore:
    """Adapter AlertHistory (port core/ports) di atas PostgreSQL.

    Semua method membuka koneksi via connection_factory (DI) — composition
    root menyuntik koneksi yang sama dengan endpoint lain."""

    def __init__(self, connection_factory=connect):
        self.connection_factory = connection_factory

    def record(self, alert, now_ms):
        return record(alert, now_ms, self.connection_factory)

    def list(self, limit=50, cursor=None, camera_id=None, equipment_instance_id=None,
             operator_id=None, kind=None, status=None, from_ms=None, to_ms=None):
        return list_alerts(limit=limit, cursor=cursor,
                           camera_id=camera_id,
                           equipment_instance_id=equipment_instance_id,
                           operator_id=operator_id, kind=kind, status=status,
                           from_ms=from_ms, to_ms=to_ms,
                           connection_factory=self.connection_factory)

    def get(self, alert_id):
        return get_alert(alert_id, self.connection_factory)

    def route_operator(self, equipment_instance_id):
        """Operator ter-assign AKTIF untuk equipment itu (None bila tak ada)."""
        return route_operator(equipment_instance_id, self.connection_factory)

    def record_deliveries(self, alert_id, operator_id, now_ms, status='sent'):
        """Baris delivery per perangkat operator yang bersangkutan.

        status 'sent' hanya boleh bila pemanggil (AlertServer._deliver) sudah
        memastikan operator punya stream hidup (presence bus); 'failed' =
        tidak terjangkau saat publish. Baris 'watch' kelak dilaporkan iPhone
        (WatchConnectivity), bukan karangan server."""
        return record_deliveries(alert_id, operator_id, now_ms, status,
                                 self.connection_factory)

    def resolve_binding_operator(self, equipment_instance_id):
        """Operator yang sedang binding-shift untuk equipment (M7) — None bila tak ada."""
        return resolve_binding_operator(equipment_instance_id, self.connection_factory)

    def active_danger_for_equipment(self, equipment_instance_id):
        """Bahaya aktif untuk satu unit (dasar pengingat operator)."""
        return active_danger_for_equipment(equipment_instance_id,
                                           self.connection_factory)

    def active_danger_units(self):
        """Semua bahaya aktif + status respons operator (dasar pengingat)."""
        return active_danger_units(self.connection_factory)

    def summary(self, bucket='hour', from_ms=None, to_ms=None, camera_id=None,
                equipment_instance_id=None, operator_id=None, kind=None):
        """Ringkasan alert per bucket waktu (dashboard tanpa menarik semua baris)."""
        return alert_summary(bucket=bucket, from_ms=from_ms, to_ms=to_ms,
                             camera_id=camera_id,
                             equipment_instance_id=equipment_instance_id,
                             operator_id=operator_id, kind=kind,
                             connection_factory=self.connection_factory)

    def get_device_tokens(self, operator_id):
        """Daftar (device_id, push_token) milik operator (device_tokens M7)."""
        return get_device_tokens(operator_id, self.connection_factory)

    def record_delivery(self, alert_id, operator_id, now_ms, status='sent',
                        push_device_id=None):
        """Satu baris audit kanal push untuk satu ``device_tokens``."""
        return record_delivery(alert_id, operator_id, now_ms, status,
                               push_device_id,
                               self.connection_factory)

    def record_operator_response(self, equipment_instance_id, operator_id, response, now_ms):
        return record_operator_response(equipment_instance_id, operator_id, response,
                                        now_ms, self.connection_factory)

    def hse_hazard_action(self, alert_id, hse_id, hse_name, action, now_ms):
        return hse_hazard_action(alert_id, hse_id, hse_name, action, now_ms,
                                 self.connection_factory)

    def active_hazard_snapshot(self, equipment_instance_id):
        with self.connection_factory() as connection:
            return connection.execute(
                'SELECT created_at_ms, hazard_active_10s_at_ms FROM alerts '
                'WHERE equipment_instance_id=%s AND status=%s AND kind IN (%s, %s) '
                'ORDER BY created_at_ms LIMIT 1',
                (equipment_instance_id, 'active', *_DANGER_KINDS)).fetchone()

    def remove_device_token(self, device_id):
        """Forget a token APNs reports as permanently unregistered."""
        return remove_device_token(device_id, self.connection_factory)


def _row(row, deliveries=None):
    item = dict(id=str(row[0]), kind=row[1], message=row[2], cameraId=str(row[3]),
                equipmentInstanceId=row[4], personsInZone=row[5],
                occurredAtMs=row[6], status=row[7], resolvedAtMs=row[8],
                createdAtMs=row[9], operatorResponse=row[10],
                operatorRespondedAtMs=row[11],
                operatorRespondedBy=str(row[12]) if row[12] else None,
                hazardActive10sAtMs=row[13], hseOnMyWayAtMs=row[14],
                hseOnMyWayBy=str(row[15]) if row[15] else None,
                hseOnMyWayName=row[16], hseConfirmedSafeAtMs=row[17],
                hseConfirmedSafeBy=str(row[18]) if row[18] else None,
                hseConfirmedSafeName=row[19])
    if deliveries is not None:
        item['deliveries'] = deliveries
    return item


def record(alert, now_ms, connection_factory=connect):
    """Simpan satu alert; zone_cleared menutup alert aktif yang sesuai.

    `alert` = payload evaluator (kind/message/sourceId/personsInZone/
    frameTimestampMs/ equipmentInstanceId opsional). Lifecycle: hanya
    person_entered_zone/person_high_risk berstatus active; event lain resolved
    saat terbit."""
    kind = alert['kind']
    camera_id = alert.get('cameraId') or alert.get('sourceId')
    if not camera_id:
        raise AlertStoreError('cameraId wajib untuk menyimpan alert')
    equipment = alert.get('equipmentInstanceId')
    occurred = alert.get('frameTimestampMs') or now_ms
    if kind == 'hazard_active_10s':
        with connection_factory() as connection:
            binding = connection.execute(
                'SELECT operation_started_at_ms FROM shift_bindings '
                'WHERE equipment_instance_id=%s FOR UPDATE', (equipment,)).fetchone()
            if binding is None or binding[0] is None or now_ms < binding[0] + 10_000:
                return None
            row = connection.execute(
                'UPDATE alerts SET hazard_active_10s_at_ms=COALESCE('
                'hazard_active_10s_at_ms, %s) WHERE equipment_instance_id=%s '
                'AND status=%s AND kind IN (%s, %s) RETURNING ' + _FIELDS,
                (now_ms, equipment, 'active', *_DANGER_KINDS)).fetchone()
            if row is None:
                return None
            item = _row(row)
            item['milestoneOnly'] = True
            return item
    status = ('active' if kind in ('person_entered_zone', 'person_high_risk')
              else 'resolved')
    try:
        with connection_factory() as connection:
            carried = (None,) * 10
            if kind == 'person_high_risk':
                replaced = connection.execute(
                    'UPDATE alerts SET status=%s, resolved_at_ms=%s '
                    'WHERE camera_id=%s AND (equipment_instance_id = %s::text '
                    '  OR (equipment_instance_id IS NULL AND %s::text IS NULL)) '
                    'AND status=%s AND kind=%s RETURNING operator_response, '
                    'operator_responded_at_ms, operator_responded_by, '
                    'hazard_active_10s_at_ms, hse_on_my_way_at_ms, hse_on_my_way_by, '
                    'hse_on_my_way_name, hse_confirmed_safe_at_ms, '
                    'hse_confirmed_safe_by, hse_confirmed_safe_name',
                    ('resolved', now_ms, camera_id, equipment, equipment,
                     'active', 'person_entered_zone')).fetchall()
                previous = [row for row in replaced if row[0] is not None or row[3] is not None]
                if previous:
                    carried = previous[0]
            elif kind == 'zone_cleared':
                connection.execute(
                    'UPDATE alerts SET status=%s, resolved_at_ms=%s '
                    'WHERE camera_id=%s AND (equipment_instance_id = %s::text '
                    '  OR (equipment_instance_id IS NULL AND %s::text IS NULL)) '
                    'AND status=%s AND kind IN (%s, %s)',
                    ('resolved', now_ms, camera_id, equipment, equipment,
                     'active', 'person_entered_zone', 'person_high_risk'))
            row = connection.execute(
                'INSERT INTO alerts(kind, message, camera_id, equipment_instance_id, '
                'persons_in_zone, occurred_at_ms, status, resolved_at_ms, created_at_ms, '
                'operator_response, operator_responded_at_ms, operator_responded_by, '
                'hazard_active_10s_at_ms, hse_on_my_way_at_ms, hse_on_my_way_by, '
                'hse_on_my_way_name, hse_confirmed_safe_at_ms, hse_confirmed_safe_by, '
                'hse_confirmed_safe_name) VALUES '
                '(%s, %s, %s, %s::text, %s, %s, %s, %s::bigint, %s, '
                '%s, %s::bigint, %s::uuid, %s::bigint, %s::bigint, %s::uuid, '
                '%s, %s::bigint, %s::uuid, %s) '
                'RETURNING ' + _FIELDS,
                (kind, alert['message'], camera_id, equipment,
                 alert.get('personsInZone', 0), occurred, status,
                 now_ms if status == 'resolved' else None, now_ms, *carried)).fetchone()
            return _row(row)
    except AlertStoreError:
        raise
    except Exception as error:
        raise AlertStoreError(str(error)) from None


def _alert_clauses(camera_id=None, equipment_instance_id=None, operator_id=None,
                    kind=None, status=None, from_ms=None, to_ms=None):
    """Filter SQL bersama untuk daftar dan ringkasan alert (klausa + argumen)."""
    clauses, arguments = [], []
    if camera_id is not None:
        clauses.append('a.camera_id = %s'); arguments.append(camera_id)
    if equipment_instance_id is not None:
        clauses.append('a.equipment_instance_id = %s'); arguments.append(equipment_instance_id)
    if operator_id is not None:
        # /me/alerts follows the operator's current, assignment-authorized shift.
        clauses.append('EXISTS (SELECT 1 FROM shift_bindings x WHERE '
                       'x.equipment_instance_id = a.equipment_instance_id '
                       'AND x.operator_id = %s)')
        arguments.append(operator_id)
    if kind is not None:
        clauses.append('a.kind = %s'); arguments.append(kind)
    if status is not None:
        clauses.append('a.status = %s'); arguments.append(status)
    if from_ms is not None:
        clauses.append('a.occurred_at_ms >= %s'); arguments.append(from_ms)
    if to_ms is not None:
        clauses.append('a.occurred_at_ms <= %s'); arguments.append(to_ms)
    return clauses, arguments


def list_alerts(limit=50, cursor=None, camera_id=None, equipment_instance_id=None, operator_id=None,
                kind=None, status=None, from_ms=None, to_ms=None,
                connection_factory=connect):
    """Riwayat terbaru dulu, keyset cursor (id terakhir); operator difilter."""
    clauses, arguments = _alert_clauses(camera_id, equipment_instance_id, operator_id,
                                        kind, status, from_ms, to_ms)
    if cursor is not None:
        try:
            occurred, _, last_id = cursor.partition(':')
            clauses.append('(a.occurred_at_ms, a.id) < (%s, %s)')
            arguments.extend((int(occurred), last_id))
        except ValueError:
            raise AlertStoreError('cursor tidak valid') from None
    where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
    limit = max(1, min(int(limit), 100))
    with connection_factory() as connection:
        rows = connection.execute(
            f'SELECT {_A_FIELDS} FROM alerts a {where} '
            'ORDER BY a.occurred_at_ms DESC, a.id DESC LIMIT %s',
            (*arguments, limit + 1)).fetchall()
        items = [_with_deliveries(connection, row) for row in rows[:limit]]
        next_cursor = (f'{rows[limit - 1][6]}:{rows[limit - 1][0]}'
                       if len(rows) > limit and items else None)
        return items, next_cursor


def alert_summary(bucket='hour', from_ms=None, to_ms=None, camera_id=None,
                  equipment_instance_id=None, operator_id=None, kind=None,
                  connection_factory=connect):
    """Ringkasan alert untuk dashboard — tanpa menarik ribuan baris.

    Mengembalikan:
      buckets     — jumlah per kind per bucket waktu ('hour' | 'day')
      totals      — jumlah per kind untuk rentang yang diminta
      top         — 10 equipment penyumbang terbanyak (KPI ISA-18.2)
      activeCount — bahaya yang masih berdiri (baris status 'active')
      maxBucket   — bucket tersibuk (untuk deteksi alarm flood)
    """
    width = 86_400_000 if bucket == 'day' else 3_600_000
    clauses, arguments = _alert_clauses(camera_id, equipment_instance_id, operator_id,
                                        kind, None, from_ms, to_ms)
    where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
    with connection_factory() as connection:
        rows = connection.execute(
            f'SELECT (a.occurred_at_ms / %s) * %s AS bucket_start, a.kind, count(*) '
            f'FROM alerts a {where} GROUP BY 1, a.kind ORDER BY 1',
            (width, width, *arguments)).fetchall()
        top_rows = connection.execute(
            f'SELECT a.equipment_instance_id, count(*) FROM alerts a {where} '
            'GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 10', tuple(arguments)).fetchall()
        active = connection.execute(
            f'SELECT count(*) FROM alerts a {where} AND a.status = %s'
            if where else 'SELECT count(*) FROM alerts a WHERE a.status = %s',
            (*arguments, 'active')).fetchone()[0]
    buckets = []
    for start, alert_kind, count in rows:
        if not buckets or buckets[-1]['bucketStartMs'] != int(start):
            buckets.append({'bucketStartMs': int(start), 'counts': {}, 'total': 0})
        buckets[-1]['counts'][alert_kind] = int(count)
        buckets[-1]['total'] += int(count)
    totals = {}
    for bucket_row in buckets:
        for alert_kind, count in bucket_row['counts'].items():
            totals[alert_kind] = totals.get(alert_kind, 0) + count
    max_bucket = max(buckets, key=lambda row: row['total']) if buckets else None
    return {
        'bucket': bucket,
        'fromMs': from_ms,
        'toMs': to_ms,
        'buckets': buckets,
        'totals': totals,
        'top': [{'equipmentInstanceId': row[0], 'count': int(row[1])} for row in top_rows],
        'activeCount': int(active),
        'maxBucket': (None if max_bucket is None else
                      {'bucketStartMs': max_bucket['bucketStartMs'],
                       'total': max_bucket['total']}),
    }


def get_alert(alert_id, connection_factory=connect):
    """Detail satu alert + deliveries; None bila tak dikenal."""
    with connection_factory() as connection:
        row = connection.execute('SELECT ' + _FIELDS + ' FROM alerts WHERE id=%s',
                                 (alert_id,)).fetchone()
        if row is None:
            return None
        return _with_deliveries(connection, row)


def route_operator(equipment_instance_id, connection_factory=connect):
    """Operator ter-assign AKTIF untuk equipment (routing M6c2, api-draft 4.8).

    Query langsung ke tabel assignments (schema-territory store, bukan
    service assignments) — None bila tak ada assignment aktif."""
    with connection_factory() as connection:
        row = connection.execute(
            'SELECT operator_id FROM assignments WHERE equipment_instance_id=%s '
            'AND status=%s', (equipment_instance_id, 'active')).fetchone()
        return str(row[0]) if row else None


def resolve_binding_operator(equipment_instance_id, connection_factory=connect):
    """Operator yang sedang binding-shift untuk equipment (M7, api-draft 4.6).

    Routing memeriksa ulang tiga gate saat kirim: assignment aktif, sesi login
    aktif, dan operasi sudah dimulai. Migration 008 guarantees one operator per
    equipmentInstanceId. None bila salah satu gate tidak terpenuhi."""
    with connection_factory() as connection:
        row = connection.execute(
            'SELECT b.operator_id FROM shift_bindings b '
            'JOIN assignments a ON a.equipment_instance_id=b.equipment_instance_id '
            'AND a.operator_id=b.operator_id AND a.status=%s '
            'JOIN sessions s ON s.user_id=b.operator_id AND s.expires_at>now() '
            'WHERE b.equipment_instance_id=%s '
            'AND b.operation_started_at_ms IS NOT NULL LIMIT 1',
            ('active', equipment_instance_id)).fetchone()
        return str(row[0]) if row else None


def active_danger_for_equipment(equipment_instance_id, connection_factory=connect):
    """Bahaya yang masih berdiri untuk satu unit (baris status `active`).

    Dipakai reminder (alarm management 16 Nov): insiden dianggap berjalan
    sampai `zone_cleared` menutupnya ATAU operator merespons
    (`operator_response` terisi). None bila tak ada bahaya aktif."""
    if not equipment_instance_id:
        return None
    with connection_factory() as connection:
        row = connection.execute(
            'SELECT id, kind, message, status, created_at_ms, operator_response, '
            'operator_responded_at_ms FROM alerts '
            'WHERE equipment_instance_id=%s::text AND status=%s '
            'ORDER BY created_at_ms DESC LIMIT 1',
            (equipment_instance_id, 'active')).fetchone()
    if row is None:
        return None
    return dict(id=str(row[0]), kind=row[1], message=row[2], status=row[3],
                createdAtMs=row[4], operatorResponse=row[5],
                operatorRespondedAtMs=row[6])


def active_danger_units(connection_factory=connect):
    """Semua bahaya yang masih berdiri (baris status `active`) + respons operator.

    Dipakai pengingat (alarm management 16 Nov): untuk tiap insiden aktif,
    cek apakah operator sudah merespons (`operator_response` terisi) —
    kalau sudah, pengingat berhenti."""
    with connection_factory() as connection:
        rows = connection.execute(
            'SELECT id, kind, message, equipment_instance_id, persons_in_zone, '
            'created_at_ms, operator_response, operator_responded_at_ms FROM alerts '
            'WHERE status=%s ORDER BY created_at_ms', ('active',)).fetchall()
    return [dict(id=str(row[0]), kind=row[1], message=row[2],
                 equipmentInstanceId=row[3], personsInZone=row[4],
                 createdAtMs=row[5], operatorResponse=row[6],
                 operatorRespondedAtMs=row[7]) for row in rows]


def remove_device_token(device_id, connection_factory=connect):
    """Remove an APNs token rejected as permanently unregistered (HTTP 410)."""
    with connection_factory() as connection:
        row = connection.execute(
            'DELETE FROM device_tokens WHERE id=%s RETURNING id', (device_id,)).fetchone()
        return row is not None


def get_device_tokens(operator_id, connection_factory=connect):
    """Daftar (id, push_token) device operator (device_tokens M7)."""
    with connection_factory() as connection:
        return connection.execute(
            'SELECT id, push_token FROM device_tokens WHERE operator_id=%s',
            (operator_id,)).fetchall()


def record_delivery(alert_id, operator_id, now_ms, status='sent',
                    push_device_id=None,
                    connection_factory=connect):
    """Satu baris delivery kanal push, tertaut ke token target bila masih ada."""
    with connection_factory() as connection:
        connection.execute(
            'INSERT INTO alert_deliveries(alert_id, device_id, push_device_id, '
            'operator_id, target, status, at_ms, channel, push_device_ref) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
            (alert_id, None, push_device_id, operator_id, 'iphone', status,
             now_ms, 'push', str(push_device_id) if push_device_id else None))
        return 1


def record_deliveries(alert_id, operator_id, now_ms, status='sent',
                     connection_factory=connect):
    """Satu audit kanal live per operator yang menjadi target route."""
    with connection_factory() as connection:
        try:
            connection.execute(
                'INSERT INTO alert_deliveries(alert_id, device_id, operator_id, '
                'target, status, at_ms, channel) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s)',
                (alert_id, None, operator_id, 'iphone', status, now_ms, 'live'))
        except Exception as error:
            raise AlertStoreError(str(error)) from None
        return 1


def record_operator_response(equipment_instance_id, operator_id, response, now_ms,
                             connection_factory=connect):
    """Record one operator answer for the current active hazard episode."""
    if response not in OPERATOR_RESPONSES:
        raise OperatorResponseError('invalid_response')
    with connection_factory() as connection:
        binding = connection.execute(
            'SELECT operation_started_at_ms FROM shift_bindings '
            'WHERE equipment_instance_id=%s AND operator_id=%s FOR UPDATE',
            (equipment_instance_id, operator_id)).fetchone()
        if binding is None or binding[0] is None or now_ms < binding[0]:
            raise OperatorResponseError('operation_not_active')
        active = connection.execute(
            'SELECT operator_response, operator_responded_by FROM alerts '
            'WHERE equipment_instance_id=%s AND status=%s AND kind IN (%s, %s)',
            (equipment_instance_id, 'active', *_DANGER_KINDS)).fetchall()
        if not active:
            raise OperatorResponseError('no_active_hazard')
        if (any(row[0] is not None for row in active)
                and all(row[0] == response and str(row[1]) == str(operator_id)
                        for row in active if row[0] is not None)):
            rows = connection.execute(
                'SELECT ' + _FIELDS + ' FROM alerts WHERE equipment_instance_id=%s '
                'AND status=%s AND kind IN (%s, %s) AND operator_response=%s',
                (equipment_instance_id, 'active', *_DANGER_KINDS, response)).fetchall()
            return [_row(row) for row in rows]
        if any(row[0] is not None for row in active):
            raise OperatorResponseError('already_responded')
        rows = connection.execute(
            'UPDATE alerts SET operator_response=%s, operator_responded_at_ms=%s, '
            'operator_responded_by=%s WHERE equipment_instance_id=%s AND status=%s '
            'AND kind IN (%s, %s) AND operator_response IS NULL RETURNING ' + _FIELDS,
            (response, now_ms, operator_id, equipment_instance_id, 'active',
             *_DANGER_KINDS)).fetchall()
        if not rows:
            raise OperatorResponseError('already_responded')
        return [_row(row) for row in rows]


def hse_hazard_action(alert_id, hse_id, hse_name, action, now_ms,
                      connection_factory=connect):
    """Claim or validate an answered hazard without clearing camera state."""
    if action not in ('on_my_way', 'confirmed_safe'):
        raise OperatorResponseError('invalid_action')
    with connection_factory() as connection:
        current = connection.execute(
            'SELECT a.equipment_instance_id, a.operator_response, a.hse_on_my_way_by, '
            'a.hse_confirmed_safe_at_ms, a.hse_confirmed_safe_by, '
            'b.operation_started_at_ms FROM alerts a JOIN shift_bindings b '
            'ON b.equipment_instance_id=a.equipment_instance_id '
            'WHERE a.id=%s AND a.status=%s AND a.kind IN (%s, %s) FOR UPDATE OF a, b',
            (alert_id, 'active', *_DANGER_KINDS)).fetchone()
        if current is None:
            raise OperatorResponseError('no_active_hazard')
        equipment_id, response, claimed_by, confirmed_at, confirmed_by, started = current
        if started is None or now_ms < started:
            raise OperatorResponseError('operation_not_active')
        if response is None:
            raise OperatorResponseError('operator_response_required')
        if action == 'on_my_way':
            if response != 'needs_hse_help':
                raise OperatorResponseError('action_not_available')
            if claimed_by is not None:
                if str(claimed_by) != str(hse_id):
                    raise OperatorResponseError('already_claimed')
                rows = connection.execute(
                    'SELECT ' + _FIELDS + ' FROM alerts WHERE equipment_instance_id=%s '
                    'AND status=%s AND kind IN (%s, %s)',
                    (equipment_id, 'active', *_DANGER_KINDS)).fetchall()
                return [_row(row) for row in rows]
            rows = connection.execute(
                'UPDATE alerts SET hse_on_my_way_at_ms=%s, hse_on_my_way_by=%s, '
                'hse_on_my_way_name=%s WHERE equipment_instance_id=%s AND status=%s '
                'AND kind IN (%s, %s) AND operator_response=%s '
                'AND hse_on_my_way_by IS NULL RETURNING ' + _FIELDS,
                (now_ms, hse_id, hse_name, equipment_id, 'active', *_DANGER_KINDS,
                 'needs_hse_help')).fetchall()
            if not rows:
                raise OperatorResponseError('already_claimed')
        else:
            if confirmed_at is not None and confirmed_at >= started:
                if str(confirmed_by) != str(hse_id):
                    raise OperatorResponseError('already_confirmed')
                rows = connection.execute(
                    'SELECT ' + _FIELDS + ' FROM alerts WHERE equipment_instance_id=%s '
                    'AND status=%s AND kind IN (%s, %s)',
                    (equipment_id, 'active', *_DANGER_KINDS)).fetchall()
                return [_row(row) for row in rows]
            if response == 'needs_hse_help' and str(claimed_by) != str(hse_id):
                raise OperatorResponseError('hse_claim_required')
            rows = connection.execute(
                'UPDATE alerts SET hse_confirmed_safe_at_ms=%s, hse_confirmed_safe_by=%s, '
                'hse_confirmed_safe_name=%s WHERE equipment_instance_id=%s AND status=%s '
                'AND kind IN (%s, %s) AND operator_response IS NOT NULL '
                'AND (hse_confirmed_safe_at_ms IS NULL OR hse_confirmed_safe_at_ms < %s) '
                'RETURNING ' + _FIELDS,
                (now_ms, hse_id, hse_name, equipment_id, 'active', *_DANGER_KINDS,
                 started)).fetchall()
            if not rows:
                raise OperatorResponseError('already_confirmed')
        return [_row(row) for row in rows]


def resolve_stale_actives(now_ms=None, connection_factory=connect):
    """Boot hygiene 12 Nov 2026: tutup alert 'active' yang tertinggal.

    State hazard evaluator hanya hidup di memori — alert yang berstatus
    'active' saat server berhenti tidak pernah menerima 'zone_cleared'
    dan jadi bangkai yang di-replay app operator sebagai notifikasi
    'danger' setiap sync (kasus 12 Nov: 19 baris lama menyalakan klaxon
    dua kali tiap app dibuka). Saat boot, SEMUA alert person-* yang masih
    'active' ditutup diam-diam sebagai resolved — evaluator baru akan
    menerbitkan ulang dalam hitungan ratusan milidetik kalau memang orang
    masih ada di zona (histeresis konfirmasi 0.25 s).

    Return jumlah baris yang ditutup (untuk log boot).
    """
    now = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    with connection_factory() as connection:
        row = connection.execute(
            "UPDATE alerts SET status='resolved', resolved_at_ms=%s "
            "WHERE status='active' AND kind IN ('person_entered_zone', 'person_high_risk') "
            "RETURNING id", (now,)).fetchall()
    return len(row)
