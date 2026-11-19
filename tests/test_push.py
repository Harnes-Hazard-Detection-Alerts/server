"""Tests M7 push: registrasi device self-service + binding shift + pengiriman push.

Unit (tanpa DB): PushSender mode mock/prod; store resolve_binding_operator
dengan fake connection. Integration (DB-gated): alur penuh operator mendaftar
device, binding shift, frame diproses pipeline -> push -> delivery kanal push.
"""
import http.client
import json
import os
import sys
import time
import unittest
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = 1794000100000  # epoch-ms bebas-kuari; hanya angka berurut yang dipakai
PASSWORD = 'temporary-test-password'


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows=()):
        self.rows = rows
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, args=None):
        self.statements.append((sql, args))
        return FakeCursor(self.rows)


class FakeAPNsResponse:
    def __init__(self, status_code=200, reason=None, apns_id='apns-test-id'):
        self.status_code = status_code
        self._reason = reason
        self.headers = {'apns-id': apns_id}

    def json(self):
        return {'reason': self._reason} if self._reason else {}


class FakeAPNsClient:
    def __init__(self, responses=None):
        self.responses = list(responses or [FakeAPNsResponse()])
        self.requests = []
        self.closed = False

    def post(self, url, *, headers, json):
        self.requests.append((url, headers, json))
        return self.responses.pop(0) if self.responses else FakeAPNsResponse()

    def close(self):
        self.closed = True


class PushSenderUnitTests(unittest.TestCase):
    APNS_ENV = {
        'APNS_KEY_PATH': '/private/tmp/AuthKey_TEST.p8',
        'APNS_KEY_ID': 'A1B2C3D4E5',
        'APNS_TEAM_ID': 'Z9Y8X7W6V5',
        'APNS_BUNDLE_ID': 'com.anything.harnes',
        'APNS_ENVIRONMENT': 'development',
    }

    def test_mock_mode_returns_ok_and_mock(self):
        from Sources.alerts.push import PushSender
        result = PushSender(mode='mock').send(
            'd' * 64, {'kind': 'person_entered_zone', 'message': 'Danger'})
        self.assertTrue(result['ok'])
        self.assertTrue(result.get('mock'))

    def test_zone_clear_payload_is_not_time_sensitive_danger(self):
        from Sources.alerts.push import PushSender
        payload = PushSender._payload({
            'kind': 'zone_cleared', 'message': 'Area aman'})
        self.assertEqual(payload['aps']['alert']['title'], 'Zone Clear')
        self.assertEqual(payload['aps']['category'], 'ZONE_CLEAR')
        self.assertEqual(payload['aps']['interruption-level'], 'active')
        self.assertEqual(payload['aps']['sound'], 'default')

    def test_prod_reuses_provider_token_then_refreshes_after_fifty_minutes(self):
        from Sources.alerts.push import PushSender
        now = [1_000]
        encoded = []

        def encode(payload, key, *, algorithm, headers):
            encoded.append((payload, key, algorithm, headers))
            return f'jwt-{len(encoded)}'

        client = FakeAPNsClient()
        with patch.dict(os.environ, self.APNS_ENV, clear=False):
            sender = PushSender(mode='prod', client=client,
                                clock=lambda: now[0], token_encoder=encode,
                                key_loader=lambda path: 'PRIVATE KEY')
            first = sender.send('d' * 64, {
                'kind': 'person_entered_zone', 'message': 'Danger',
                'equipmentInstanceId': 'unit-1', 'personsInZone': 2})
            now[0] += 20 * 60
            second = sender.send('e' * 64, {
                'kind': 'person_entered_zone', 'message': 'Danger'})
            now[0] += 31 * 60
            third = sender.send('f' * 64, {
                'kind': 'person_entered_zone', 'message': 'Danger'})

        self.assertTrue(first['ok'])
        self.assertTrue(second['ok'])
        self.assertTrue(third['ok'])
        self.assertEqual(len(encoded), 2)
        self.assertEqual(encoded[0][0], {'iss': 'Z9Y8X7W6V5', 'iat': 1_000})
        self.assertEqual(encoded[0][2], 'ES256')
        self.assertEqual(encoded[0][3], {'kid': 'A1B2C3D4E5'})
        self.assertEqual(client.requests[0][1]['authorization'], 'bearer jwt-1')
        self.assertEqual(client.requests[1][1]['authorization'], 'bearer jwt-1')
        self.assertEqual(client.requests[2][1]['authorization'], 'bearer jwt-2')

    def test_prod_uses_sandbox_headers_and_time_sensitive_payload(self):
        from Sources.alerts.push import PushSender
        client = FakeAPNsClient()
        with patch.dict(os.environ, self.APNS_ENV, clear=False):
            sender = PushSender(mode='prod', client=client, clock=lambda: 1_000,
                                token_encoder=lambda *args, **kwargs: 'jwt',
                                key_loader=lambda path: 'PRIVATE KEY')
            result = sender.send('a' * 64, {
                'kind': 'person_entered_zone', 'message': 'Stop now',
                'equipmentInstanceId': 'unit-1', 'personsInZone': 1})

        url, headers, payload = client.requests[0]
        self.assertEqual(url, 'https://api.sandbox.push.apple.com/3/device/' + 'a' * 64)
        self.assertEqual(headers['apns-topic'], 'com.anything.harnes')
        self.assertEqual(headers['apns-push-type'], 'alert')
        self.assertEqual(headers['apns-priority'], '10')
        self.assertEqual(headers['apns-expiration'], '0')
        self.assertEqual(payload['aps']['sound'], PushSender.DANGER_SOUND)
        self.assertEqual(payload['aps']['interruption-level'], 'time-sensitive')
        self.assertEqual(payload['aps']['category'], 'REMOTE_DANGER')
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['apns_id'], 'apns-test-id')

    def test_prod_maps_apns_rejection_and_closes_client(self):
        from Sources.alerts.push import PushSender
        client = FakeAPNsClient([
            FakeAPNsResponse(429, 'TooManyProviderTokenUpdates', 'failed-apns-id')])
        env = dict(self.APNS_ENV, APNS_ENVIRONMENT='production')
        with patch.dict(os.environ, env, clear=False):
            sender = PushSender(mode='prod', client=client, clock=lambda: 1_000,
                                token_encoder=lambda *args, **kwargs: 'jwt',
                                key_loader=lambda path: 'PRIVATE KEY')
            result = sender.send('b' * 64, {
                'kind': 'person_entered_zone', 'message': 'Danger'})
            sender.close()

        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 429)
        self.assertEqual(result['reason'], 'TooManyProviderTokenUpdates')
        self.assertEqual(result['apns_id'], 'failed-apns-id')
        self.assertTrue(client.requests[0][0].startswith('https://api.push.apple.com/'))
        self.assertTrue(client.closed)


class StorePushUnitTests(unittest.TestCase):
    def test_resolve_binding_operator_known_and_unknown(self):
        from Sources.alerts import store
        operator_id = uuid4()
        connection = FakeConnection([(operator_id,)])
        known = store.resolve_binding_operator('cam01-eq-1', lambda: connection)
        self.assertEqual(known, str(operator_id))
        sql, args = connection.statements[0]
        normalized = ' '.join(sql.split())
        self.assertIn('JOIN assignments a', normalized)
        self.assertIn('a.status=%s', normalized)
        self.assertIn('JOIN sessions s', normalized)
        self.assertIn('s.expires_at>now()', normalized)
        self.assertIn('b.operation_started_at_ms IS NOT NULL', normalized)
        self.assertEqual(args, ('active', 'cam01-eq-1'))
        unknown = store.resolve_binding_operator('cam99-x', lambda: FakeConnection([]))
        self.assertIsNone(unknown)

    def test_get_device_tokens_returns_rows(self):
        from Sources.alerts import store
        device_id, token = uuid4(), 'd' * 64
        rows = store.get_device_tokens(
            'operator', lambda: FakeConnection([(device_id, token)]))
        self.assertEqual(rows, [(device_id, token)])


class PushRoutingUnitTests(unittest.TestCase):
    def test_worker_rechecks_runtime_gates_before_token_lookup(self):
        from Sources.alerts.server import AlertServer
        store = Mock()
        store.resolve_binding_operator.return_value = None
        server = AlertServer.__new__(AlertServer)
        server.context = SimpleNamespace(store=store)

        server._push_sync({
            'id': str(uuid4()),
            'kind': 'person_entered_zone',
            'equipmentInstanceId': 'cam01-eq-1',
        }, 'operator-1', BASE)

        store.resolve_binding_operator.assert_called_once_with('cam01-eq-1')
        store.get_device_tokens.assert_not_called()


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1',
                     'Requires local PostgreSQL')
class PushPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        from Sources.alerts.store import AlertStore
        from Sources.alerts.server import AlertServer
        from Sources.alerts.bus import EventBus
        from Sources.api.context import AppContext
        from Sources.auth.service import AuthService
        from Sources.auth.limits import LoginLimits
        from Sources.infrastructure.database.connection import connect, database_url
        from Sources.infrastructure.database.migrate import migrate
        from Sources.users.create import create_user

        cls.admin = psycopg.connect('dbname=postgres host=/tmp', autocommit=True)
        name = 'safety_push_test_' + uuid4().hex
        cls.admin.execute(sql.SQL('CREATE DATABASE {} OWNER safety_app').format(sql.Identifier(name)))

        def factory():
            return connect(make_conninfo(database_url(), dbname=name))

        cls.factory = staticmethod(factory)
        cls.camera1 = uuid4()
        cls.camera2 = uuid4()
        cls.unit1 = f'{str(cls.camera1)[:8]}-eq-1'
        cls.unit2 = f'{str(cls.camera2)[:8]}-eq-1'
        cls.stale_unit = 'unit-stale'
        with factory() as connection:
            migrate(connection)
            connection.execute(
                'INSERT INTO cameras(id, name, source, registered_at_ms) '
                'VALUES (%s, %s, %s, %s), (%s, %s, %s, %s)',
                (cls.camera1, 'Push camera 1', '/private/tmp/push-camera-1.mp4', BASE,
                 cls.camera2, 'Push camera 2', '/private/tmp/push-camera-2.mp4', BASE))
            connection.execute(
                'INSERT INTO equipment_instances(equipment_instance_id, name) '
                'VALUES (%s, %s), (%s, %s), (%s, %s)',
                (cls.unit1, 'Push unit 1', cls.unit2, 'Push unit 2',
                 cls.stale_unit, 'Stale unit'))

        cls.hse = create_user('push-hse', 'HSE push', 'hse', PASSWORD, factory)
        cls.op1 = create_user('push-op1', 'Op Satu', 'operator', PASSWORD, factory)

        risk = Mock()
        risk.evaluate.return_value = {
            'kind': 'person_entered_zone', 'message': 'Person di zona excavator',
            'sourceId': str(cls.camera1),
            'equipmentInstanceId': cls.unit1, 'personsInZone': 2}
        risk.unit_statuses.return_value = []

        cls.server = AlertServer(('127.0.0.1', 0), context=AppContext(
            auth=AuthService(factory), login_limits=LoginLimits(),
            risk=risk,
            detector=SimpleNamespace(ready=True, detect=lambda frame: frame),
            bus=EventBus(), connect=factory, store=AlertStore(factory),
            enable_test_alerts=True))
        cls.server._push_instance = Mock()
        cls.server._push_instance.send.return_value = {
            'ok': True, 'mock': False, 'status': 200,
            'reason': None, 'apns_id': 'integration-apns-id'}
        cls.worker = Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

        def stop():
            cls.server.shutdown()
            cls.server.server_close()
            cls.worker.join(timeout=5)

        def drop_database():
            cls.admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
            cls.admin.close()

        cls.addClassCleanup(drop_database)
        cls.addClassCleanup(stop)

    def token(self, username):
        from Sources.auth.service import AuthService
        return AuthService(self.factory).login(username, PASSWORD)['access_token']

    def request(self, method, path, body=None, token=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=15)
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw) if raw else None

    def _delivery_rows(self):
        with self.factory() as connection:
            return connection.execute(
                'SELECT alert_id, device_id, push_device_id, operator_id, target, '
                'status, at_ms, channel, push_device_ref '
                "FROM alert_deliveries WHERE channel='push' ORDER BY at_ms").fetchall()

    def test_device_registration_shift_binding_and_push_delivery(self):
        op = self.token('push-op1')
        hse = self.token('push-hse')
        self.assertEqual(self.request('POST', '/assignments', {
            'equipmentInstanceId': self.unit1, 'operatorId': self.op1,
        }, token=hse)[0], 201)

        # HSE tidak boleh mendaftarkan device operator.
        status, _ = self.request('POST', '/me/device',
                                 {'pushToken': 'd' * 64, 'model': '', 'osVersion': '',
                                  'appVersion': ''}, token=hse)
        self.assertEqual(status, 403)

        # 1. Operator registrasi device (POST /me/device -> 201).
        status, payload = self.request('POST', '/me/device', {
            'pushToken': 'd' * 64, 'model': 'iPhone 16',
            'osVersion': '18.1', 'appVersion': '1.0.0'}, token=op)
        self.assertEqual(status, 201)
        self.assertEqual(payload['operatorId'], self.op1)
        device_id = payload['id']

        # 2. Operator set binding shift (PUT /me/shift -> 200).
        status, payload = self.request('PUT', '/me/shift',
                                       {'onShiftFor': [self.unit1]}, token=op)
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['onShiftFor'], [self.unit1])

        # 3. Binding saja belum boleh menjadi route push: operasi belum dimulai.
        with self.factory() as connection:
            rows = connection.execute(
                'SELECT operator_id, equipment_instance_id FROM shift_bindings').fetchall()
            self.assertEqual([(str(r[0]), r[1]) for r in rows], [(self.op1, self.unit1)])
        from Sources.alerts.store import resolve_binding_operator
        self.assertIsNone(resolve_binding_operator(self.unit1, self.factory))

        # 4. Start Operation melengkapi gate assignment + login + operasi.
        status, payload = self.request('PUT', '/me/operation',
                                       {'active': True}, token=op)
        self.assertEqual(status, 200)
        self.assertTrue(payload['active'])
        self.assertEqual(resolve_binding_operator(self.unit1, self.factory), self.op1)

        # 5. Endpoint test HSE memakai pipeline asli -> push -> delivery.
        status, payload = self.request('POST', '/test/alert', {
            'equipmentInstanceId': self.unit1,
            'state': 'danger',
            'message': 'Person di zona excavator',
        }, token=hse)
        self.assertEqual(status, 202)
        self.assertEqual(payload['kind'], 'person_entered_zone')
        deadline = time.monotonic() + 5
        rows = self._delivery_rows()
        while not rows and time.monotonic() < deadline:
            time.sleep(0.02)
            rows = self._delivery_rows()
        self.assertEqual(len(rows), 1)
        (alert_id, delivery_device_id, push_device_id, operator_id, target,
         status, _, channel, push_device_ref) = rows[0]
        self.assertIsNone(delivery_device_id)   # push target hidup di device_tokens
        self.assertEqual(str(push_device_id), device_id)
        self.assertEqual(str(operator_id), self.op1)
        self.assertEqual(target, 'iphone')
        self.assertEqual(status, 'sent')
        self.assertEqual(channel, 'push')
        self.assertEqual(push_device_ref, device_id)

        # 6. GET /me/devices -> token termask (prefix 8 + …).
        status, payload = self.request('GET', '/me/devices', token=op)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload['items']), 1)
        self.assertEqual(payload['items'][0]['id'], device_id)
        self.assertEqual(payload['items'][0]['pushToken'], 'd' * 8 + '…')

        # 7. DELETE /me/devices/{id} -> 200.
        status, payload = self.request('DELETE', f'/me/devices/{device_id}', token=op)
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])

        with self.factory() as connection:
            audit_target = connection.execute(
                "SELECT push_device_id, push_device_ref FROM alert_deliveries "
                "WHERE channel='push' ORDER BY at_ms LIMIT 1").fetchone()
        self.assertIsNone(audit_target[0])
        self.assertEqual(audit_target[1], device_id)

        status, payload = self.request('GET', '/me/devices', token=op)
        self.assertEqual(payload['items'], [])

        # 8. Operator tanpa device -> tidak ada delivery push tercatat.
        from Sources.users.create import create_user
        self.op2 = create_user('push-op2', 'Op Dua', 'operator', PASSWORD, self.factory)
        op2 = self.token('push-op2')
        self.assertEqual(self.request('POST', '/assignments', {
            'equipmentInstanceId': self.unit2, 'operatorId': self.op2,
        }, token=hse)[0], 201)
        status, _ = self.request('PUT', '/me/shift',
                                 {'onShiftFor': [self.unit2]}, token=op2)
        self.assertEqual(status, 200)
        self.assertEqual(self.request('PUT', '/me/operation',
                                      {'active': True}, token=op2)[0], 200)
        self.server.context.risk.evaluate.return_value = {
            'kind': 'person_entered_zone', 'message': 'Person di zona excavator',
            'sourceId': str(self.camera2),
            'equipmentInstanceId': self.unit2, 'personsInZone': 1}
        self.server.process_frame([], timestamp_ms=BASE + 1000,
                                  source_id=str(self.camera2), now_ms=BASE + 1000)
        with self.factory() as connection:
            total = connection.execute(
                "SELECT count(*) FROM alert_deliveries WHERE channel='push'").fetchone()[0]
            op2_rows = connection.execute(
                "SELECT 1 FROM alert_deliveries WHERE operator_id=%s AND channel='push'",
                (self.op2,)).fetchall()
        self.assertEqual(total, 1)          # hanya push-op1 yang terekam
        self.assertEqual(op2_rows, [])

        # 9. Token yang sama berpindah atomik ke akun terbaru, tidak boleh
        # terdaftar ke dua operator sekaligus.
        status, payload = self.request('POST', '/me/device', {
            'pushToken': 'd' * 64, 'model': 'iPhone 16',
            'osVersion': '18.1', 'appVersion': '1.0.0'}, token=op2)
        self.assertEqual(status, 201)
        with self.factory() as connection:
            owners = connection.execute(
                'SELECT operator_id FROM device_tokens WHERE push_token=%s',
                ('d' * 64,)).fetchall()
        self.assertEqual([str(row[0]) for row in owners], [self.op2])

    def test_expired_binding_can_be_claimed_by_active_operator(self):
        from Sources.auth.service import AuthService
        from Sources.users.create import create_user

        stale_id = create_user('push-stale', 'Stale Operator', 'operator',
                               PASSWORD, self.factory)
        active_id = create_user('push-active', 'Active Operator', 'operator',
                                PASSWORD, self.factory)
        auth = AuthService(self.factory)
        stale = auth.login('push-stale', PASSWORD)['access_token']
        active = auth.login('push-active', PASSWORD)['access_token']
        hse = auth.login('push-hse', PASSWORD)['access_token']
        self.assertEqual(self.request('POST', '/assignments', {
            'equipmentInstanceId': self.stale_unit, 'operatorId': stale_id,
        }, token=hse)[0], 201)
        status, _ = self.request('PUT', '/me/shift',
                                 {'onShiftFor': [self.stale_unit]}, token=stale)
        self.assertEqual(status, 200)
        with self.factory() as connection:
            connection.execute(
                "UPDATE sessions SET created_at=now()-interval '2 minutes', "
                "expires_at=now()-interval '1 minute' "
                'WHERE user_id=%s', (stale_id,))

        self.assertEqual(self.request('POST', '/assignments', {
            'equipmentInstanceId': self.stale_unit, 'operatorId': active_id,
        }, token=hse)[0], 201)
        status, payload = self.request('PUT', '/me/shift',
                                       {'onShiftFor': [self.stale_unit]}, token=active)
        self.assertEqual(status, 200)
        self.assertEqual(payload['onShiftFor'], [self.stale_unit])
        with self.factory() as connection:
            owner = connection.execute(
                'SELECT operator_id FROM shift_bindings '
                'WHERE equipment_instance_id=%s', (self.stale_unit,)).fetchone()
        self.assertEqual(str(owner[0]), active_id)


if __name__ == '__main__':
    unittest.main()
