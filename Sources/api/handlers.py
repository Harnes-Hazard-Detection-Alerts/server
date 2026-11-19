"""Endpoint JSON: sesi, direktori, kamera, dan streaming HSE.

Kontrak mengikuti AppleApp/Contracts/api-draft.md (4.2/4.5/4.6) — draft
tim; perubahan bentuk respons = ubah kontrak dulu di Contracts.
Semua akses non-/health lewat sesi (Bearer); role diperiksa per endpoint.
"""
import base64
import json
import logging
from queue import Empty, Queue
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4
import time
from http.server import BaseHTTPRequestHandler

import psycopg

from ..assignments import service as assignments_service
from ..assignments.service import AssignmentError
from ..auth.service import AuthConflict, Unauthorized
from ..cameras import service as cameras_service
from ..cameras.service import CameraError
from ..cameras.evidence import EvidenceRecorder
from ..core.operator_response import (OPERATOR_RESPONSE_EVENT,
                                      HSE_HAZARD_ACTION_EVENT,
                                      OperatorResponseError)
from ..equipment import labels as equipment_labels
from ..equipment import registry as equipment_registry
from ..equipment.operation import OperationError, set_operation_active
from ..equipment.registry import EquipmentError
from ..equipment.zone import MAX_RADIUS_M, MIN_RADIUS_M
from ..users import admin as users_admin
from ..users.admin import UserError
from ..users.directory import get_item, list_users
from ..calls.service import CallError
from .websocket import handle_call_audio, handle_websocket, is_upgrade_request


class BadRequest(Exception):
    pass


_ERROR_STATUS = {
    'not_found': 404,
    'operator_inactive': 422,
    'assignment_mismatch': 409,
    'equipment_already_assigned': 409,
    'operator_already_assigned': 409,
    'file_not_found': 404,
    'already_exists': 409,
    'self_deactivate_forbidden': 400,
    'rtsp_not_implemented': 501,
    'camera_offline': 503,
    'camera_already_assigned': 409,
    'active_assignment': 409,
    'active_hazard': 409,
    'unit_operating': 409,
    'invalid_source': 400,
    'camera_has_alerts': 409,
    'encode_failed': 500,
    'invalid_response': 400,
    'no_active_hazard': 409,
    'already_responded': 409,
    'invalid_action': 400,
    'operator_response_required': 409,
    'action_not_available': 409,
    'already_claimed': 409,
    'already_confirmed': 409,
    'hse_claim_required': 409,
    'call_in_progress': 409,
    'no_operator': 409,
    'operator_offline': 409,
    'invalid_state': 409,
    'forbidden': 403,
}


def _now_ms():
    return time.time_ns() // 1_000_000


def _int_or_none(text):
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        raise BadRequest() from None


def _cursor_or_none(text):
    """Cursor keyset 'occurredAtMs:id-uuid' — validasi ketat sebelum ke store."""
    if text is None or text == '':
        return None
    occurred, _, last_id = text.partition(':')
    try:
        int(occurred)
        UUID(last_id)
    except (ValueError, AttributeError, TypeError):
        raise BadRequest() from None
    return text


_EQUIPMENT_ID_CHARS = frozenset(
    'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-')


class Handler(BaseHTTPRequestHandler):
    @staticmethod
    def _equipment_id(path):
        """equipmentInstanceId dari '/equipment/{id}/zone' — string tracking stabil."""
        equipment_id = path[len('/equipment/'):-len('/zone')]
        if not 1 <= len(equipment_id) <= 200 or not set(equipment_id) <= _EQUIPMENT_ID_CHARS:
            raise BadRequest()
        return equipment_id

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def json_response(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if status == 401:
            self.send_header('WWW-Authenticate', 'Bearer')
        if status == 429:
            self.send_header('Retry-After', '60')
        self.end_headers()
        self.wfile.write(body)

    def bytes_response(self, status, content_type, body, etag=None, cache='no-store'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        if etag:
            self.send_header('ETag', etag)
        self.end_headers()
        self.wfile.write(body)

    def error(self, status, code):
        self.json_response(status, {'error': {'code': code}})

    def bearer(self):
        values = self.headers.get_all('Authorization', [])
        if len(values) != 1:
            raise Unauthorized()
        scheme, separator, token = values[0].partition(' ')
        if not separator or scheme.lower() != 'bearer' or len(token) != 43 or not token.isascii():
            raise Unauthorized()
        return token

    def read_json(self, limit=4096):
        """Body JSON object <= limit byte (pola read_login digeneralisasi)."""
        if self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding'):
            raise BadRequest()
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1:
            raise BadRequest()
        try:
            length = int(lengths[0])
            if not 0 < length <= limit:
                raise BadRequest()
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise BadRequest()
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError, TimeoutError):
            raise BadRequest() from None
        if not isinstance(body, dict):
            raise BadRequest()
        return body

    def read_login(self):
        body = self.read_json()
        if set(body) != {'username', 'password'}:
            raise BadRequest()
        if not isinstance(body['username'], str) or not 1 <= len(body['username']) <= 100:
            raise BadRequest()
        if not isinstance(body['password'], str) or not 1 <= len(body['password']) <= 256:
            raise BadRequest()
        return body

    @staticmethod
    def _text(body, key, low, high):
        value = body.get(key)
        if not isinstance(value, str) or not low <= len(value.strip()) <= high:
            raise BadRequest()
        return value.strip()

    @staticmethod
    def _uuid_text(text):
        try:
            return str(UUID(text))
        except (ValueError, AttributeError, TypeError):
            raise BadRequest() from None

    def _user(self, role=None):
        user = self.server.context.auth.authenticate(self.bearer())
        if role is not None and user['role'] != role:
            self.error(403, 'forbidden')
            return None
        return user

    def _user_admin_item(self, context, core):
        """Respons manajemen akun = objek user, bentuk sama dengan GET /users (4.2)."""
        item = get_item(core['id'], online_ids=self.online,
                        connection_factory=context.connect)
        if item is None:  # race: baris hilang antara mutasi dan pembacaan ulang
            return {**core, 'activeAssignments': 0, 'devicesOnline': 0}
        return item

    def query(self):
        return parse_qs(urlsplit(self.path).query)

    @property
    def online(self):
        """Presence operator (stream SSE hidup) dari bus — untuk listing."""
        bus = getattr(self.server.context, 'bus', None)
        return bus.online_operators() if bus is not None else set()

    # -- kamera helpers (api-draft 4.3) --------------------------------------
    def _camera(self, context, camera_id):
        camera = cameras_service.get_camera(camera_id,
                                            connection_factory=context.connect)
        if camera is None:
            raise CameraError('not_found')
        return camera

    def _operator_assignment(self, context, user):
        items = assignments_service.list_active_for_operator(
            user['id'], online_ids=self.online, connection_factory=context.connect)
        return items[0] if items else None

    def _operator_equipment(self, context, assignment):
        equipment_id = assignment['equipmentInstanceId']
        camera = equipment_registry.camera_for_equipment(
            equipment_id, connection_factory=context.connect)
        label = equipment_labels.get_label(equipment_id, connection_factory=context.connect)
        settings = context.zone_settings
        zone = settings.item(equipment_id) if settings is not None else None
        return {
            'equipmentInstanceId': equipment_id,
            'name': label or (camera['name'] if camera else equipment_id),
            'cameraName': camera['name'] if camera else None,
            'zoneRadiusM': zone['zoneRadiusM'] if zone else None,
            'shiftActive': bool(assignment.get('shiftActive')),
            'shiftStartedAtMs': assignment.get('shiftStartedAtMs'),
        }

    def _camera_or_404(self, context, path, suffix=''):
        rest = path[len('/cameras/'):][:-len(suffix)] if suffix else path[len('/cameras/'):]
        return self._camera(context, self._uuid_text(rest))

    def _camera_from_body(self, context):
        body = self.read_json()
        name = self._text(body, 'name', 1, 100)
        location = None
        if body.get('location') is not None:
            location = self._text(body, 'location', 1, 100)
        type_ = body.get('type', 'recording')
        if type_ not in ('recording', 'rtsp'):
            raise BadRequest()
        source = self._text(body, 'source', 1, 400)
        if type_ == 'recording' and not source.startswith('/'):
            raise BadRequest()  # path absolut file di Mac (api-draft 4.3)
        if type_ == 'rtsp' and not source.startswith('rtsp://'):
            raise BadRequest()  # URL RTSP live (api-draft 4.3)
        playback_fps = body.get('playbackFps')
        if playback_fps is not None and (not isinstance(playback_fps, int)
                                         or not 1 <= playback_fps <= 60):
            raise BadRequest()
        loop = body.get('loop', False)
        if not isinstance(loop, bool):
            raise BadRequest()
        return cameras_service.create_camera(name, location, type_, source,
                                             playback_fps, loop, _now_ms(),
                                             connection_factory=context.connect)

    def _handle_domain_error(self, error):
        status = _ERROR_STATUS.get(error.code, 400)
        self.error(status, error.code)

    def _calls_or_503(self, context):
        calls = getattr(context, 'calls', None)
        if calls is None:
            self.error(503, 'calls_unavailable')
        return calls

    def _post_call(self, context, path):
        """POST /calls (HSE mulai) dan /calls/{id}/answer|decline|end."""
        if path == '/calls':
            user = self._user(role='hse')
            if user is None:
                return
            calls = self._calls_or_503(context)
            if calls is None:
                return
            body = self.read_json()
            if set(body) != {'equipmentInstanceId'}:
                raise BadRequest()
            equipment_id = body['equipmentInstanceId']
            if (not isinstance(equipment_id, str) or not 1 <= len(equipment_id) <= 200
                    or not set(equipment_id) <= _EQUIPMENT_ID_CHARS):
                raise BadRequest()
            self.json_response(201, calls.start(user, equipment_id))
            return
        _, _, call_id, action = path.split('/')
        if action not in ('answer', 'decline', 'end'):
            self.error(404, 'not_found')
            return
        user = self._user()
        calls = self._calls_or_503(context)
        if calls is None:
            return
        self.json_response(200, getattr(calls, action)(self._uuid_text(call_id), user))

    def _get_call(self, context, path):
        """GET /calls/{id} dan GET /calls/{id}/audio (WebSocket)."""
        parts = path.split('/')
        if len(parts) not in (3, 4) or (len(parts) == 4 and parts[3] != 'audio'):
            self.error(404, 'not_found')
            return
        call_id = self._uuid_text(parts[2])
        user = self._user()
        calls = self._calls_or_503(context)
        if calls is None:
            return
        if len(parts) == 3:
            self.json_response(200, calls.get(call_id, user))
            return
        if not is_upgrade_request(self.headers):
            raise BadRequest()
        call, role, leg = calls.open_audio(call_id, user)
        handle_call_audio(self, calls, call, role, leg)

    def _publish_assignment(self, payload, operator_id=None):
        bus = getattr(self.server.context, 'bus', None)
        if bus is not None and hasattr(bus, 'publish_typed'):
            bus.publish_typed('assignment.status', payload,
                              route_operator_id=operator_id)

    def do_POST(self):
        context = self.server.context
        path = urlsplit(self.path).path
        try:
            if path == '/auth/login':
                # login_limits None = mode demo tanpa throttle (main.py memutuskan)
                if context.login_limits is None:
                    pass
                elif not context.login_limits.allow(self.client_address[0]):
                    self.error(429, 'rate_limited')
                    return
                body = self.read_login()
                if context.login_limits is not None and not context.login_limits.workers.acquire(blocking=False):
                    self.error(429, 'rate_limited')
                    return
                try:
                    result = context.auth.login(body['username'], body['password'])
                finally:
                    if context.login_limits is not None:
                        context.login_limits.workers.release()
                if result['user']['role'] == 'operator':
                    if context.operation_status is not None:
                        context.operation_status.reload()
                    assignments = assignments_service.list_active_for_operator(
                        result['user']['id'], online_ids=self.online,
                        connection_factory=context.connect)
                    if assignments:
                        self._publish_assignment(assignments[0],
                                                 operator_id=result['user']['id'])
                self.json_response(200, result)
            elif path == '/auth/logout':
                token = self.bearer()
                user = context.auth.authenticate(token)
                if user['role'] == 'operator':
                    with context.connect() as connection:
                        operating = connection.execute(
                            'SELECT 1 FROM shift_bindings WHERE operator_id=%s '
                            'AND operation_started_at_ms IS NOT NULL LIMIT 1',
                            (user['id'],)).fetchone()
                    if operating:
                        raise OperationError('unit_operating')
                context.auth.logout(token)
                if user['role'] == 'operator':
                    if context.operation_status is not None:
                        context.operation_status.reload()
                    assignments = assignments_service.list_active_for_operator(
                        user['id'], online_ids=self.online,
                        connection_factory=context.connect)
                    if assignments:
                        self._publish_assignment(assignments[0], operator_id=user['id'])
                self.json_response(200, {'ok': True})
            elif path == '/users':
                if self._user(role='hse') is None:
                    return
                body = self.read_json()
                username = self._text(body, 'username', 1, 100)
                if not users_admin.USERNAME_PATTERN.fullmatch(username):
                    raise BadRequest()
                display_name = self._text(body, 'displayName', 1, 200)
                role = body.get('role')
                if role not in ('operator', 'hse'):
                    raise BadRequest()
                password = body.get('password')
                if not isinstance(password, str) or not 12 <= len(password) <= 256:
                    raise BadRequest()  # api-draft 4.2: password 12–256, tanpa trim
                core = users_admin.create_account(
                    username, display_name, role, password,
                    connection_factory=context.connect)
                self.json_response(201, self._user_admin_item(context, core))
            elif path == '/assignments':
                if self._user(role='hse') is None:
                    return
                body = self.read_json()
                equipment_id = self._text(body, 'equipmentInstanceId', 1, 200)
                operator_id = self._uuid_text(body.get('operatorId'))
                assignment = assignments_service.create_assignment(
                    equipment_id, operator_id, _now_ms(),
                    connection_factory=context.connect)
                created = assignment.pop('created')
                item = assignments_service.get_assignment(
                    assignment['id'], online_ids=self.online,
                    connection_factory=context.connect) or assignment
                self._publish_assignment(item, operator_id=operator_id)
                self.json_response(201 if created else 200, item)
            elif path == '/cameras':
                if self._user(role='hse') is None:
                    return
                camera = self._camera_from_body(context)
                # Register = langsung proses (keputusan owner 13 Nov 2026):
                # thread pembaca server menyala seketika setelah kamera
                # tersimpan. Bila sumber belum siap (mis. produser RTSP belum
                # jalan), open gagal 3x beruntun → status `error` +
                # healthNote `fallback` — HSE start ulang dari app setelah
                # produser siap (POST /cameras/{id}/start tetap tersedia).
                context.cameras.start(camera)
                self.json_response(201, self._camera(context, camera['id']))
            elif path == '/equipment':
                user = self._user(role='hse')
                if user is None:
                    return
                body = self.read_json()
                if set(body) != {'cameraId', 'name'}:
                    raise BadRequest()
                camera_id = self._uuid_text(body.get('cameraId'))
                name = self._text(body, 'name', 1, 100)
                item = equipment_registry.create_from_camera(
                    camera_id, name, user['id'], _now_ms(), context.connect)
                self.json_response(201, item)
            elif path.startswith('/cameras/') and path.endswith('/start'):
                if self._user(role='hse') is None:
                    return
                camera = self._camera_or_404(context, path, suffix='/start')
                context.cameras.start(camera)
                self.json_response(200, self._camera(context, camera['id']))
            elif path.startswith('/cameras/') and path.endswith('/stop'):
                if self._user(role='hse') is None:
                    return
                camera = self._camera_or_404(context, path, suffix='/stop')
                context.cameras.stop(camera['id'])
                self.json_response(200, self._camera(context, camera['id']))
            elif path == '/me/device':
                user = self._user(role='operator')
                if user is None:
                    return
                body = self.read_json()
                push_token = self._text(body, 'pushToken', 64, 200)
                model = self._text(body, 'model', 0, 100)
                os_version = self._text(body, 'osVersion', 0, 50)
                app_version = self._text(body, 'appVersion', 0, 50)
                target = body.get('target', 'iphone')
                if target not in ('iphone', 'watch'):
                    raise BadRequest()
                # Token APNs adalah identitas perangkat global. ON CONFLICT
                # memindahkan kepemilikannya secara atomik bila iPhone yang sama
                # login sebagai operator lain; satu operator menyimpan paling
                # banyak satu token aktif per target (iPhone dan Watch).
                with context.connect() as connection:
                    row = connection.execute(
                        'DELETE FROM device_tokens WHERE operator_id=%s '
                        'AND target=%s AND push_token<>%s',
                        (user['id'], target, push_token))
                    row = connection.execute(
                        'INSERT INTO device_tokens(operator_id, push_token, model, '
                        'os_version, app_version, registered_at_ms, target) '
                        'VALUES (%s, %s, %s, %s, %s, %s, %s) '
                        'ON CONFLICT (push_token) DO UPDATE SET '
                        'operator_id=EXCLUDED.operator_id, model=EXCLUDED.model, '
                        'os_version=EXCLUDED.os_version, '
                        'app_version=EXCLUDED.app_version, '
                        'target=EXCLUDED.target, '
                        'registered_at_ms=EXCLUDED.registered_at_ms '
                        'RETURNING id, registered_at_ms',
                        (user['id'], push_token, model, os_version, app_version,
                         _now_ms(), target)).fetchone()
                self.json_response(201, {'id': str(row[0]), 'operatorId': user['id'],
                                         'target': target})
            elif path == '/me/hazard/response':
                # Jawaban operator saat bahaya bertahan >= 10 dtk (api-draft 4.7b).
                # Catatan untuk HSE; status alert tetap ditentukan kamera.
                user = self._user(role='operator')
                if user is None:
                    return
                if context.store is None:
                    self.error(503, 'database_unavailable')
                    return
                body = self.read_json()
                if set(body) != {'response'} or not isinstance(body['response'], str):
                    raise BadRequest()
                assignments = assignments_service.list_active_for_operator(
                    user['id'], connection_factory=context.connect)
                if not assignments:
                    raise OperationError('assignment_mismatch')
                equipment_id = assignments[0]['equipmentInstanceId']
                now = _now_ms()
                updated = context.store.record_operator_response(
                    equipment_id, user['id'], body['response'], now)
                payload = {
                    'equipmentInstanceId': equipment_id,
                    'alertIds': [item['id'] for item in updated],
                    'response': body['response'],
                    'respondedAtMs': now,
                    'operatorId': user['id'],
                    'operatorName': user.get('display_name') or user.get('displayName')
                                    or user.get('username'),
                }
                bus = getattr(context, 'bus', None)
                if bus is not None and hasattr(bus, 'publish_typed'):
                    bus.publish_typed(OPERATOR_RESPONSE_EVENT, payload,
                                      route_operator_id=user['id'])
                self.json_response(200, payload)
            elif path.startswith('/alerts/') and path.endswith('/hse-action'):
                actor = self._user(role='hse')
                if actor is None:
                    return
                if context.store is None:
                    self.error(503, 'database_unavailable')
                    return
                alert_id = self._uuid_text(path[len('/alerts/'):-len('/hse-action')])
                body = self.read_json()
                if set(body) != {'action'} or body.get('action') not in ('on_my_way', 'confirmed_safe'):
                    raise BadRequest()
                now = _now_ms()
                updated = context.store.hse_hazard_action(
                    alert_id, actor['id'], actor.get('display_name') or actor.get('username'),
                    body['action'], now)
                equipment_id = updated[0]['equipmentInstanceId']
                is_claim = body['action'] == 'on_my_way'
                payload = {
                    'equipmentInstanceId': equipment_id,
                    'alertIds': [item['id'] for item in updated],
                    'action': body['action'],
                    'atMs': updated[0]['hseOnMyWayAtMs'] if is_claim
                            else updated[0]['hseConfirmedSafeAtMs'],
                    'hseId': updated[0]['hseOnMyWayBy'] if is_claim
                            else updated[0]['hseConfirmedSafeBy'],
                    'hseName': updated[0]['hseOnMyWayName'] if is_claim
                               else updated[0]['hseConfirmedSafeName'],
                }
                bus = getattr(context, 'bus', None)
                if bus is not None and hasattr(bus, 'publish_typed'):
                    operator_id = context.store.resolve_binding_operator(equipment_id)
                    bus.publish_typed(HSE_HAZARD_ACTION_EVENT, payload,
                                      route_operator_id=operator_id)
                self.json_response(200, payload)
            elif path == '/test/alert':
                if not getattr(context, 'enable_test_alerts', False):
                    self.error(404, 'not_found')
                    return
                if self._user(role='hse') is None:
                    return
                body = self.read_json()
                equipment = self._text(body, 'equipmentInstanceId', 1, 200)
                camera_value = body.get('cameraId')
                if camera_value is not None:
                    camera_id = self._uuid_text(camera_value)
                else:
                    prefix = equipment.split('-eq-', 1)[0]
                    with context.connect() as connection:
                        camera_row = connection.execute(
                            'SELECT id FROM cameras WHERE left(id::text, 8)=%s',
                            (prefix,)).fetchone()
                    if camera_row is None:
                        raise BadRequest()
                    camera_id = str(camera_row[0])
                state = self._text(body, 'state', 4, 6)
                if state not in ('danger', 'safe'):
                    raise BadRequest()
                default_message = ('Worker entered the hazard zone.' if state == 'danger'
                                   else 'The hazard zone is clear.')
                message = body.get('message', default_message)
                if not isinstance(message, str) or not 1 <= len(message) <= 300:
                    raise BadRequest()
                now = _now_ms()
                kind = 'person_entered_zone' if state == 'danger' else 'zone_cleared'
                stored = self.server.emit_alert({
                    'kind': kind,
                    'message': message,
                    'sourceId': camera_id,
                    'equipmentInstanceId': equipment,
                    'personsInZone': 1 if state == 'danger' else 0,
                    'frameTimestampMs': now,
                }, source_id=camera_id, now_ms=now)
                self.json_response(202, {
                    'ok': True,
                    'kind': kind,
                    'alertId': stored.get('id') if stored else None,
                })
            elif path == '/calls' or (path.startswith('/calls/') and path.count('/') == 3):
                self._post_call(context, path)
            else:
                self.error(404, 'not_found')
        except BadRequest:
            self.error(400, 'invalid_request')
        except Unauthorized:
            self.error(401, 'invalid_credentials' if path == '/auth/login' else 'unauthorized')
        except CallError as error:
            self._handle_domain_error(error)
        except AuthConflict as error:
            self._handle_domain_error(error)
        except CameraError as error:
            self._handle_domain_error(error)
        except UserError as error:
            self._handle_domain_error(error)
        except AssignmentError as error:
            self._handle_domain_error(error)
        except (EquipmentError, OperationError, OperatorResponseError) as error:
            self._handle_domain_error(error)
        except psycopg.errors.UniqueViolation:
            # Username dipakai (index unik lower(username), api-draft 4.2) → 409.
            self.error(409, 'already_exists')
        except (psycopg.Error, ValueError):
            self.error(503, 'database_unavailable')

    def do_GET(self):
        context = self.server.context
        path = urlsplit(self.path).path
        try:
            if path == '/health':
                detector = context.detector
                self.json_response(200, dict(ok=True, service='alert-server',
                                             modelLoaded=detector is not None and detector.ready,
                                             serverTimeUnixMs=_now_ms()))
            elif path == '/auth/me':
                self.json_response(200, {'user': context.auth.authenticate(self.bearer())})
            elif path == '/users':
                if self._user(role='hse') is None:
                    return
                query = self.query()
                role = query.get('role', [None])[0]
                if role is not None and role not in ('operator', 'hse'):
                    raise BadRequest()
                self.json_response(200, {'items': list_users(role=role, online_ids=self.online,
                                                             connection_factory=context.connect)})
            elif path == '/assignments':
                if self._user(role='hse') is None:
                    return
                status = self.query().get('status', ['active'])[0]
                if status not in ('active', 'ended'):
                    raise BadRequest()
                self.json_response(200, {'items': assignments_service.list_assignments(
                    status=status, online_ids=self.online,
                    connection_factory=context.connect)})
            elif path.startswith('/equipment/') and path.endswith('/zone'):
                # GET /equipment/{id}/zone (api-draft 4.4a) — radius Safety Zone unit.
                if self._user(role='hse') is None:
                    return
                settings = context.zone_settings
                if settings is None:
                    self.error(404, 'not_found')
                    return
                self.json_response(200, settings.item(self._equipment_id(path)))
            elif path == '/equipment/available-cameras':
                if self._user(role='hse') is None:
                    return
                self.json_response(200, {'items': equipment_registry.available_cameras(context.connect)})
            elif path == '/equipment/status':
                if self._user(role='hse') is None:
                    return
                statuses = []
                for item in equipment_registry.list_equipment(context.connect):
                    camera = equipment_registry.camera_for_equipment(
                        item['equipmentInstanceId'], context.connect)
                    current = context.risk.unit_statuses(camera['id']) if camera else []
                    if current:
                        statuses.extend(current)
                    else:
                        active = (context.operation_status.is_active(item['equipmentInstanceId'])
                                  if context.operation_status is not None else False)
                        statuses.append({
                            'equipmentInstanceId': item['equipmentInstanceId'],
                            'zoneStatus': 'unknown' if active else 'clear',
                            'monitoringStatus': 'unknown' if active else 'idle',
                            'operationActive': active,
                            'active': False, 'lastInZone': None if active else 0,
                            'lastInHigh': None if active else 0, 'lastSeenMs': None,
                        })
                self.json_response(200, {'items': statuses})
            elif path == '/equipment':
                # GET /equipment (api-draft 4.4b) — nama tampilan unit yang diatur HSE.
                if self._user(role='hse') is None:
                    return
                self.json_response(200, {'items': equipment_registry.list_equipment(context.connect)})
            elif path == '/me/equipment':
                # GET /me/equipment (api-draft 4.4b) — unit milik operator: nama,
                # kamera, radius Safety Zone. Null bila belum ada assignment aktif.
                user = self._user(role='operator')
                if user is None:
                    return
                assignment = self._operator_assignment(context, user)
                self.json_response(200, {'equipment': self._operator_equipment(
                    context, assignment) if assignment else None})
            elif path == '/me/equipment/snapshot.jpg':
                # Gambar kamera unit sendiri, HANYA saat shift aktif di unit itu
                # (keputusan owner 11 Nov 2026: pengecualian sempit P5 — operator
                # tetap tidak bisa membuka /cameras*).
                user = self._user(role='operator')
                if user is None:
                    return
                assignment = self._operator_assignment(context, user)
                if assignment is None or not assignment.get('shiftActive'):
                    self.error(403, 'forbidden')
                    return
                camera = equipment_registry.camera_for_equipment(
                    assignment['equipmentInstanceId'], connection_factory=context.connect)
                snap = context.cameras.snapshot(camera['id'], annotated=True) if camera else None
                if snap is None:
                    self.error(503, 'camera_offline')
                    return
                body, etag = snap
                self.bytes_response(200, 'image/jpeg', body, etag=etag, cache='no-store')
            elif path == '/me/assignment':
                user = self._user(role='operator')
                if user is None:
                    return
                items = assignments_service.list_active_for_operator(
                    user['id'], online_ids=self.online,
                    connection_factory=context.connect)
                self.json_response(200, {'assignment': items[0] if items else None})
            elif path == '/me/devices':
                user = self._user(role='operator')
                if user is None:
                    return
                with context.connect() as connection:
                    rows = connection.execute(
                        'SELECT id, push_token, model, os_version, app_version, '
                        'registered_at_ms, target FROM device_tokens WHERE operator_id=%s',
                        (user['id'],)).fetchall()
                items = [{'id': str(r[0]), 'pushToken': r[1][:8] + '…', 'model': r[2],
                          'osVersion': r[3], 'appVersion': r[4], 'registeredAtMs': r[5],
                          'target': r[6]}
                         for r in rows]
                self.json_response(200, {'items': items})
            elif path == '/cameras':
                if self._user(role='hse') is None:
                    return
                self.json_response(200, {'items': cameras_service.list_cameras(
                    connection_factory=context.connect)})
            elif path.startswith('/cameras/'):
                if self._user(role='hse') is None:
                    return
                rest = path[len('/cameras/'):]
                if rest.endswith('/snapshot.jpg'):
                    camera = self._camera_or_404(context, path, suffix='/snapshot.jpg')
                    query = self.query()
                    annotated = query.get('annotated', ['true'])[0] != 'false'
                    snap = context.cameras.snapshot(camera['id'], annotated=annotated)
                    if snap is None:
                        self.error(503, 'camera_offline')
                        return
                    body, etag = snap
                    self.bytes_response(200, 'image/jpeg', body, etag=etag,
                                         cache='max-age=5')
                    return
                if rest.endswith('/stream.mjpeg'):
                    # Live MJPEG feed (api-draft 4.3 S2, teruji 10 Nov 2026):
                    # multipart/x-mixed-replace, frame per iterasi playback.
                    # Route ini hilang saat merge origin/dev (mereka belum
                    # memilikinya); dipulihkan dari backup/premerge-2026-11-10.
                    camera = self._camera_or_404(context, path, suffix='/stream.mjpeg')
                    # Frame beku dianggap mati: produser RTSP berhenti / kamera
                    # tidak mengirim lagi → 503 supaya app menampilkan status
                    # offline, bukan frame terakhir yang tampak masih live.
                    if (context.cameras.snapshot(camera['id']) is None
                            or not context.cameras.fresh(camera['id'])):
                        self.error(503, 'camera_offline')
                        return
                    query = self.query()
                    annotated = query.get('annotated', ['true'])[0] != 'false'
                    self.send_response(200)
                    self.send_header('Content-Type',
                                     'multipart/x-mixed-replace; boundary=frame')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    try:
                        for body in context.cameras.stream_frames(camera['id'],
                                                                  annotated=annotated):
                            header = (f'--frame\r\nContent-Type: image/jpeg\r\n'
                                      f'Content-Length: {len(body)}\r\n\r\n').encode()
                            self.wfile.write(header)
                            self.wfile.write(body)
                            self.wfile.write(b'\r\n')
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass  # klien menutup stream — jalur normal
                    return
                camera_id = self._uuid_text(rest)
                self.json_response(200, self._camera(context, camera_id))
            elif path == '/alerts':
                if self._user(role='hse') is None:
                    return
                if context.store is None:
                    self.error(503, 'database_unavailable')
                    return
                query = self.query()
                camera_id = query.get('cameraId', [None])[0]
                try:
                    limit = int(query.get('limit', ['50'])[0])
                except ValueError:
                    raise BadRequest() from None
                items, next_cursor = context.store.list(
                    limit=limit, cursor=_cursor_or_none(query.get('cursor', [None])[0]),
                    camera_id=self._uuid_text(camera_id) if camera_id else None,
                    equipment_instance_id=query.get('equipmentInstanceId', [None])[0],
                    kind=query.get('kind', [None])[0],
                    status=query.get('status', [None])[0],
                    from_ms=_int_or_none(query.get('fromMs', [None])[0]),
                    to_ms=_int_or_none(query.get('toMs', [None])[0]))
                payload = {'items': items}
                if next_cursor:
                    payload['nextCursor'] = next_cursor
                self.json_response(200, payload)
            elif path == '/alerts/summary':
                # Ringkasan per bucket waktu — dashboard tidak perlu menarik
                # ribuan baris alert saat insiden menumpuk.
                if self._user(role='hse') is None:
                    return
                if context.store is None:
                    self.error(503, 'database_unavailable')
                    return
                query = self.query()
                bucket = query.get('bucket', ['hour'])[0]
                if bucket not in ('hour', 'day'):
                    raise BadRequest()
                camera_id = query.get('cameraId', [None])[0]
                self.json_response(200, context.store.summary(
                    bucket=bucket,
                    from_ms=_int_or_none(query.get('fromMs', [None])[0]),
                    to_ms=_int_or_none(query.get('toMs', [None])[0]),
                    camera_id=self._uuid_text(camera_id) if camera_id else None,
                    equipment_instance_id=query.get('equipmentInstanceId', [None])[0],
                    kind=query.get('kind', [None])[0]))
            elif path.startswith('/alerts/') and path.endswith('/evidence'):
                if self._user(role='hse') is None:
                    return
                if context.store is None:
                    self.error(503, 'database_unavailable')
                    return
                parts = path.strip('/').split('/')
                if len(parts) != 3:
                    raise BadRequest()
                alert_id = self._uuid_text(parts[1])
                alert = context.store.get(alert_id)
                if alert is None:
                    self.error(404, 'not_found')
                    return
                clips = EvidenceRecorder().list_for_alert(alert_id)
                source_id = alert.get('sourceId')
                clip = next((item for item in clips if item['camera_id'] == source_id),
                            clips[0] if clips else None)
                if clip is None:
                    self.error(404, 'not_found')
                    return
                timestamps = clip['metadata'].get('timestampsMs', [])
                frames = []
                for index, file_path in enumerate(clip['files']):
                    try:
                        with open(file_path, 'rb') as evidence_file:
                            jpeg = evidence_file.read()
                    except OSError:
                        continue
                    timestamp = timestamps[index] if index < len(timestamps) else None
                    frames.append({'timestampMs': timestamp,
                                   'jpegBase64': base64.b64encode(jpeg).decode('ascii')})
                if not frames:
                    self.error(404, 'not_found')
                    return
                self.json_response(200, {
                    'alertId': alert_id,
                    'cameraId': clip['camera_id'],
                    'durationMs': int(clip['metadata'].get('span', 0)),
                    'frames': frames,
                })
            elif path.startswith('/alerts/'):
                if self._user(role='hse') is None:
                    return
                if context.store is None:
                    self.error(503, 'database_unavailable')
                    return
                item = context.store.get(self._uuid_text(path.split('/', 2)[2]))
                if item is None:
                    self.error(404, 'not_found')
                    return
                self.json_response(200, item)
            elif path == '/me/alerts':
                user = self._user(role='operator')
                if user is None:
                    return
                if context.store is None:
                    self.error(503, 'database_unavailable')
                    return
                query = self.query()
                try:
                    limit = int(query.get('limit', ['50'])[0])
                except ValueError:
                    raise BadRequest() from None
                items, next_cursor = context.store.list(
                    limit=limit, operator_id=user['id'],
                    cursor=_cursor_or_none(query.get('cursor', [None])[0]),
                    status=query.get('status', [None])[0])
                payload = {'items': items}
                if next_cursor:
                    payload['nextCursor'] = next_cursor
                self.json_response(200, payload)
            elif path == '/ws' and is_upgrade_request(self.headers):
                # WebSocket kanal live (api-draft 4.8 — S6/M7b): SSE transisi tetap /events.
                token = self.bearer()
                user = context.auth.authenticate(token)
                if user['role'] not in ('hse', 'operator'):
                    self.error(403, 'forbidden')
                    return
                handle_websocket(
                    self, context.bus, user['role'], user.get('id'),
                    reauthenticate=lambda: context.auth.authenticate(token),
                    connection_factory=context.connect)
                return
            elif path.startswith('/calls/'):
                self._get_call(context, path)
            elif path == '/events':
                token = self.bearer()
                user = context.auth.authenticate(token)
                if user['role'] not in ('hse', 'operator'):
                    self.error(403, 'forbidden')
                    return
                self.stream_events(token)
            else:
                self.error(404, 'not_found')
        except BadRequest:
            self.error(400, 'invalid_request')
        except Unauthorized:
            self.error(401, 'unauthorized')
        except CallError as error:
            self._handle_domain_error(error)
        except CameraError as error:
            self._handle_domain_error(error)
        except (psycopg.Error, ValueError):
            self.error(503, 'database_unavailable')

    def do_DELETE(self):
        context = self.server.context
        path = urlsplit(self.path).path
        try:
            if path.startswith('/cameras/'):
                if self._user(role='hse') is None:
                    return
                camera = self._camera_or_404(context, path)
                context.cameras.stop(camera['id'])
                removed = cameras_service.delete_camera(
                    camera['id'], connection_factory=context.connect)
                if removed is None:
                    self.error(404, 'not_found')
                    return
                context.cameras.forget(camera['id'])
                self.json_response(200, {'ok': True, 'id': removed})
            elif path.startswith('/me/devices/'):
                user = self._user(role='operator')
                if user is None:
                    return
                device_id = self._uuid_text(path[len('/me/devices/'):])
                with context.connect() as connection:
                    row = connection.execute(
                        'DELETE FROM device_tokens WHERE id=%s AND operator_id=%s '
                        'RETURNING id', (device_id, user['id'])).fetchone()
                if row is None:
                    self.error(404, 'not_found')
                    return
                self.json_response(200, {'ok': True, 'id': str(row[0])})
            elif path.startswith('/assignments/'):
                if self._user(role='hse') is None:
                    return
                assignment_id = self._uuid_text(path[len('/assignments/'):])
                item = assignments_service.end_assignment(
                    assignment_id, _now_ms(), connection_factory=context.connect)
                self._publish_assignment(item, operator_id=item['operatorId'])
                self.json_response(200, item)
            elif path.startswith('/devices/'):
                user = self._user(role='operator')
                if user is None:
                    return
                device = unregister_device(self._uuid_text(path.split('/', 2)[2]),
                                           operator_id=user['id'],
                                           connection_factory=context.connect)
                self.json_response(200, dict(ok=True, id=device['id']))
            else:
                self.error(404, 'not_found')
        except Unauthorized:
            self.error(401, 'unauthorized')
        except BadRequest:
            self.error(400, 'invalid_request')
        except CameraError as error:
            self._handle_domain_error(error)
        except AssignmentError as error:
            self._handle_domain_error(error)
        except (psycopg.Error, ValueError):
            self.error(503, 'database_unavailable')

    def do_PUT(self):
        context = self.server.context
        path = urlsplit(self.path).path
        try:
            if path == '/me/shift':
                user = self._user(role='operator')
                if user is None:
                    return
                body = self.read_json()
                on_shift_for = body.get('onShiftFor', [])
                if not isinstance(on_shift_for, list) or len(on_shift_for) > 1:
                    raise BadRequest()
                equipment_ids = [self._text({'v': equipment_id}, 'v', 1, 200)
                                 for equipment_id in on_shift_for]
                # Binding menimpa nilai sebelumnya (api-draft 4.6); hangus bersama
                # sesi atau saat diganti — bukan catatan penugasan permanen.
                with context.connect() as connection:
                    running = connection.execute(
                        'SELECT equipment_instance_id FROM shift_bindings '
                        'WHERE operator_id=%s AND operation_started_at_ms IS NOT NULL '
                        'AND EXISTS (SELECT 1 FROM sessions s WHERE s.user_id=shift_bindings.operator_id '
                        'AND s.expires_at>now()) '
                        'FOR UPDATE', (user['id'],)).fetchone()
                    if running and equipment_ids != [running[0]]:
                        raise OperationError('unit_operating')
                    if equipment_ids:
                        assigned = connection.execute(
                            'SELECT 1 FROM assignments WHERE operator_id=%s '
                            'AND equipment_instance_id=%s AND status=%s',
                            (user['id'], equipment_ids[0], 'active')).fetchone()
                        if assigned is None:
                            raise AssignmentError('assignment_mismatch')
                    # Ambil alih equipment dari binding yang sesinya sudah habis.
                    # Cleanup dan insert berada di transaksi yang sama agar unique
                    # index tetap menjadi arbiter untuk dua sesi aktif bersamaan.
                    for equipment_id in equipment_ids:
                        connection.execute(
                            'DELETE FROM shift_bindings b '
                            'WHERE b.equipment_instance_id=%s AND NOT EXISTS '
                            '(SELECT 1 FROM sessions s WHERE s.user_id=b.operator_id '
                            'AND s.expires_at>now())',
                            (equipment_id,))
                    if not running:
                        connection.execute('DELETE FROM shift_bindings WHERE operator_id=%s',
                                           (user['id'],))
                        for equipment_id in equipment_ids:
                            # FK tim (014_link_equipment): binding mensyaratkan
                            # katalog equipment_instance. Assignment aktif operator
                            # sudah menjamin baris katalog ada (create_assignment
                            # memvalidasi), jadi upsert di sini hanya toleransi jalur
                            # admin langsung — id tak dikenal tetap ditolak FK.
                            connection.execute(
                                'INSERT INTO shift_bindings(operator_id, equipment_instance_id, '
                                'set_at_ms) VALUES (%s, %s, %s) '
                                'ON CONFLICT (operator_id, equipment_instance_id) '
                                'DO UPDATE SET set_at_ms=EXCLUDED.set_at_ms',
                                (user['id'], equipment_id, _now_ms()))
                if context.operation_status is not None:
                    context.operation_status.reload()
                assignments = assignments_service.list_active_for_operator(
                    user['id'], online_ids=self.online,
                    connection_factory=context.connect)
                if assignments:
                    self._publish_assignment(assignments[0], operator_id=user['id'])
                self.json_response(200, {'ok': True, 'onShiftFor': equipment_ids})
            elif path == '/me/operation':
                user = self._user(role='operator')
                if user is None:
                    return
                body = self.read_json()
                if set(body) != {'active'} or not isinstance(body['active'], bool):
                    raise BadRequest()
                assignments = assignments_service.list_active_for_operator(
                    user['id'], connection_factory=context.connect)
                if not assignments:
                    raise OperationError('assignment_mismatch')
                equipment_id = assignments[0]['equipmentInstanceId']
                operation_at = _now_ms()
                expiry = set_operation_active(user['id'], equipment_id, body['active'],
                                              operation_at, context.connect)
                assignment = assignments_service.list_active_for_operator(
                    user['id'], online_ids=self.online, connection_factory=context.connect)
                if context.operation_status is not None:
                    context.operation_status.set_cached(equipment_id, body['active'], expiry,
                                                        operation_at if body['active'] else None)
                if assignment:
                    self._publish_assignment(assignment[0], operator_id=user['id'])
                self.json_response(200, {'ok': True, 'active': body['active'],
                                         'equipmentInstanceId': equipment_id})
            elif path.startswith('/users/') and path.endswith('/password'):
                if self._user(role='hse') is None:
                    return
                # PUT /users/{id}/password (api-draft 4.2 N2-B): sesi target dicabut
                # oleh admin.set_password — reset = peristiwa keamanan.
                user_id = self._uuid_text(path[len('/users/'):-len('/password')])
                body = self.read_json()
                password = body.get('password')
                if not isinstance(password, str) or not 12 <= len(password) <= 256:
                    raise BadRequest()
                core = users_admin.set_password(
                    user_id, password, connection_factory=context.connect)
                self.json_response(200, self._user_admin_item(context, core))
            elif path.startswith('/equipment/') and path.endswith('/zone'):
                # PUT /equipment/{id}/zone {zoneRadiusM} (api-draft 4.4a) — hse.
                user = self._user(role='hse')
                if user is None:
                    return
                settings = context.zone_settings
                if settings is None:
                    self.error(404, 'not_found')
                    return
                equipment_id = self._equipment_id(path)
                body = self.read_json()
                radius = body.get('zoneRadiusM')
                if (set(body) != {'zoneRadiusM'} or isinstance(radius, bool)
                        or not isinstance(radius, (int, float))
                        or not MIN_RADIUS_M <= radius <= MAX_RADIUS_M):
                    raise BadRequest()
                self.json_response(200, settings.set(equipment_id, radius, user['id'], _now_ms()))
            else:
                self.error(404, 'not_found')
        except BadRequest:
            self.error(400, 'invalid_request')
        except Unauthorized:
            self.error(401, 'unauthorized')
        except CameraError as error:
            self._handle_domain_error(error)
        except UserError as error:
            self._handle_domain_error(error)
        except AssignmentError as error:
            self._handle_domain_error(error)
        except (EquipmentError, OperationError) as error:
            self._handle_domain_error(error)
        except psycopg.errors.UniqueViolation:
            # migrasi 008: satu operator per equipment — binding ganda ditolak.
            self.error(409, 'equipment_already_bound')
        except (psycopg.Error, ValueError):
            self.error(503, 'database_unavailable')

    def do_PATCH(self):
        context = self.server.context
        path = urlsplit(self.path).path
        try:
            if path.startswith('/users/'):
                actor = self._user(role='hse')
                if actor is None:
                    return
                user_id = self._uuid_text(path[len('/users/'):])
                body = self.read_json()
                display_name = None
                if body.get('displayName') is not None:
                    display_name = self._text(body, 'displayName', 1, 200)
                active = body.get('active')
                if active is not None and not isinstance(active, bool):
                    raise BadRequest()
                if display_name is None and active is None:
                    raise BadRequest()
                # Guard anti-lockout (keputusan 9 Nov 2026): HSE tidak boleh
                # menonaktifkan akunnya sendiri — satu-satunya HSE bisa terkunci.
                if active is False and user_id == actor['id']:
                    self.error(400, 'self_deactivate_forbidden')
                    return
                core = users_admin.update_account(
                    user_id, display_name=display_name, active=active,
                    connection_factory=context.connect)
                if active is False and context.operation_status is not None:
                    context.operation_status.reload()
                self.json_response(200, self._user_admin_item(context, core))
            elif path.startswith('/cameras/'):
                # PATCH /cameras/{id} {name?, location?} (api-draft 4.3) — hse.
                if self._user(role='hse') is None:
                    return
                camera_id = self._uuid_text(path[len('/cameras/'):])
                body = self.read_json()
                if not body or not set(body) <= {'name', 'location'}:
                    raise BadRequest()
                name = self._text(body, 'name', 1, 100) if 'name' in body else None
                location = (self._text(body, 'location', 1, 100)
                            if body.get('location') is not None else None)
                camera = cameras_service.update_camera(
                    camera_id, name=name, location=location,
                    connection_factory=context.connect)
                if camera is None:
                    raise CameraError('not_found')
                self.json_response(200, camera)
            elif path.startswith('/equipment/'):
                # PATCH /equipment/{id} {name: string|null} (api-draft 4.4b) — hse.
                user = self._user(role='hse')
                if user is None:
                    return
                equipment_id = path[len('/equipment/'):]
                if (not 1 <= len(equipment_id) <= 200
                        or not set(equipment_id) <= _EQUIPMENT_ID_CHARS):
                    raise BadRequest()
                # body: {name?, cabinPhone?}; null menghapus kolom itu.
                body = self.read_json()
                if not body or not set(body) <= set(equipment_labels.FIELDS):
                    raise BadRequest()
                changes = {}
                if 'name' in body:
                    changes['name'] = (None if body['name'] is None
                                       else self._text(body, 'name', 1, 100))
                if 'cabinPhone' in body:
                    changes['cabinPhone'] = (None if body['cabinPhone'] is None
                                             else self._text(body, 'cabinPhone', 3, 30))
                saved = equipment_labels.update_profile(
                    equipment_id, changes, user['id'], _now_ms(),
                    connection_factory=context.connect)
                self.json_response(200, {'equipmentInstanceId': equipment_id, **saved})
            else:
                self.error(404, 'not_found')
        except BadRequest:
            self.error(400, 'invalid_request')
        except Unauthorized:
            self.error(401, 'unauthorized')
        except CameraError as error:
            self._handle_domain_error(error)
        except EquipmentError as error:
            self._handle_domain_error(error)
        except UserError as error:
            self._handle_domain_error(error)
        except (psycopg.Error, ValueError):
            self.error(503, 'database_unavailable')

    def stream_events(self, token):
        stream_id = uuid4().hex[:8]
        logger = logging.getLogger("appleapp.stream")
        listener = Queue()
        bus = self.server.context.bus
        viewer = self.server.context.auth.authenticate(token)
        role, operator_id = viewer['role'], viewer.get('id')
        # operator: presence SSE — device-nya dianggap online selama stream hidup.
        bus.subscribe(listener, role=role, operator_id=operator_id
                      if role == 'operator' else None)
        logger.info("stream=%s opened role=%s", stream_id, role)
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(b': connected\n\n')
            while True:
                try:
                    event = listener.get(timeout=10)
                except Empty:
                    event = b': heartbeat\n\n'
                # Recheck before every event/heartbeat, including role changes and logout.
                if self.server.context.auth.authenticate(token)['role'] != role:
                    logger.info("stream=%s closed reason=role_changed", stream_id)
                    break
                self.wfile.write(event)
        except (OSError, Unauthorized, psycopg.Error, ValueError) as error:
            # Error messages may contain database DSNs; log only class and errno.
            logger.warning("stream=%s stopped reason=%s errno=%s", stream_id,
                           type(error).__name__, getattr(error, 'errno', None))
        finally:
            bus.unsubscribe(listener)
            logger.info("stream=%s removed", stream_id)

    def log_message(self, format, *args):
        # Do not log request targets/queries, bodies, credentials or bearer tokens.
        pass
