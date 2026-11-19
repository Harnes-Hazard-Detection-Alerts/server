"""Integration test jawaban operator saat bahaya (DB-gated; api-draft 4.7b).

POST /me/hazard/response mencatat alasan operator pada alert bahaya AKTIF
equipment yang di-assign kepadanya. Jawaban hanya catatan untuk HSE — status
alert tetap `active` sampai kamera menyatakan zone_cleared.
"""
import http.client
import json
import os
import sys
import unittest
from pathlib import Path
from queue import Queue
from threading import Thread
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

EQUIPMENT = 'resp01-eq-1'
BASE = 1794000200000


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1',
                     'Requires local PostgreSQL')
class OperatorResponseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from Sources.alerts.bus import EventBus
        from Sources.alerts.server import AlertServer
        from Sources.alerts.store import AlertStore
        from Sources.api.context import AppContext
        from Sources.auth.limits import LoginLimits
        from Sources.auth.service import AuthService
        from Sources.core.evaluator import RiskEvaluator
        from Sources.infrastructure.database.connection import connect, database_url
        from Sources.infrastructure.database.migrate import migrate
        from Sources.users.create import create_user

        cls.admin = psycopg.connect('dbname=postgres host=/tmp', autocommit=True)
        name = 'safety_response_test_' + uuid4().hex
        cls.admin.execute(sql.SQL('CREATE DATABASE {} OWNER safety_app').format(
            sql.Identifier(name)))

        def factory():
            return connect(make_conninfo(database_url(), dbname=name))
        cls.factory = staticmethod(factory)
        with factory() as connection:
            migrate(connection)
        password = 'temporary-test-password'
        cls.hse = create_user('resp-hse', 'HSE r', 'hse', password, factory)
        cls.op1 = create_user('resp-op1', 'Op Satu', 'operator', password, factory)
        cls.op2 = create_user('resp-op2', 'Op Dua', 'operator', password, factory)
        with factory() as connection:
            connection.execute(
                'INSERT INTO equipment_instances(equipment_instance_id, name) VALUES (%s, %s)',
                (EQUIPMENT, 'Response test unit'))
            cls.camera_id = connection.execute(
                'INSERT INTO cameras(name, type, status, source, registered_at_ms) '
                'VALUES (%s, %s, %s, %s, %s) RETURNING id',
                ('Response test camera', 'recording', 'offline', 'response-test.mp4', BASE)
            ).fetchone()[0]
            connection.execute(
                'INSERT INTO assignments(equipment_instance_id, operator_id, assigned_at_ms) '
                'VALUES (%s, %s, %s)', (EQUIPMENT, cls.op1, BASE))
            connection.execute(
                'INSERT INTO shift_bindings(operator_id, equipment_instance_id, set_at_ms, '
                'operation_started_at_ms) VALUES (%s, %s, %s, %s)',
                (cls.op1, EQUIPMENT, BASE, BASE))
        cls.store = AlertStore(factory)
        cls.bus = EventBus()
        cls.server = AlertServer(('127.0.0.1', 0), context=AppContext(
            auth=AuthService(factory), login_limits=LoginLimits(),
            risk=RiskEvaluator(),
            detector=SimpleNamespace(ready=True, detect=lambda frame: frame),
            bus=cls.bus, connect=factory, store=cls.store))
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

    def setUp(self):
        with self.factory() as connection:
            connection.execute('DELETE FROM alerts')

    def request(self, method, path, body=None, token=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=15)
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        connection.request(method, path, json.dumps(body) if body is not None else None,
                           headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw) if raw else None

    def token(self, username):
        from Sources.auth.service import AuthService
        return AuthService(self.factory).login(
            username, 'temporary-test-password')['access_token']

    def record(self, kind, offset):
        return self.store.record({'kind': kind, 'message': kind, 'sourceId': str(self.camera_id),
                                  'equipmentInstanceId': EQUIPMENT, 'personsInZone': 1,
                                  'frameTimestampMs': BASE + offset}, BASE + offset)

    def test_response_is_a_note_and_alert_stays_active(self):
        entered = self.record('person_entered_zone', 0)
        listener = Queue()
        self.bus.subscribe(listener, role='hse')
        self.addCleanup(self.bus.unsubscribe, listener)

        status, payload = self.request('POST', '/me/hazard/response',
                                       {'response': 'worker_needed'}, self.token('resp-op1'))
        self.assertEqual(status, 200)
        self.assertEqual(payload['alertIds'], [entered['id']])
        self.assertEqual(payload['response'], 'worker_needed')
        self.assertEqual(payload['operatorName'], 'Op Satu')

        stored = self.store.get(entered['id'])
        self.assertEqual(stored['status'], 'active')          # kamera yang menutup
        self.assertEqual(stored['operatorResponse'], 'worker_needed')
        self.assertIsNotNone(stored['operatorRespondedAtMs'])

        events = []
        while not listener.empty():
            events.append(listener.get_nowait().decode())
        self.assertTrue(any('alert.operator_response' in event for event in events))

        # Riwayat HSE ikut membawa jawaban.
        status, history = self.request('GET', '/alerts', token=self.token('resp-hse'))
        self.assertEqual(history['items'][0]['operatorResponse'], 'worker_needed')

    def test_second_answer_and_invalid_values_rejected(self):
        self.record('person_entered_zone', 0)
        token = self.token('resp-op1')
        status, payload = self.request('POST', '/me/hazard/response',
                                       {'response': 'not_a_choice'}, token)
        self.assertEqual((status, payload['error']['code']), (400, 'invalid_response'))
        status, _ = self.request('POST', '/me/hazard/response', {'response': 1}, token)
        self.assertEqual(status, 400)
        status, _ = self.request('POST', '/me/hazard/response',
                                 {'response': 'needs_hse_help'}, token)
        self.assertEqual(status, 200)
        status, payload = self.request('POST', '/me/hazard/response',
                                       {'response': 'false_detection'}, token)
        self.assertEqual((status, payload['error']['code']), (409, 'already_responded'))

    def test_requires_active_hazard_assignment_and_operator_role(self):
        status, payload = self.request('POST', '/me/hazard/response',
                                       {'response': 'worker_needed'}, self.token('resp-op1'))
        self.assertEqual((status, payload['error']['code']), (409, 'no_active_hazard'))

        self.record('person_entered_zone', 0)
        self.record('zone_cleared', 1000)                     # sudah selesai
        status, payload = self.request('POST', '/me/hazard/response',
                                       {'response': 'worker_needed'}, self.token('resp-op1'))
        self.assertEqual((status, payload['error']['code']), (409, 'no_active_hazard'))

        self.record('person_entered_zone', 2000)
        status, payload = self.request('POST', '/me/hazard/response',
                                       {'response': 'worker_needed'}, self.token('resp-op2'))
        self.assertEqual((status, payload['error']['code']), (409, 'assignment_mismatch'))
        status, _ = self.request('POST', '/me/hazard/response',
                                 {'response': 'worker_needed'}, self.token('resp-hse'))
        self.assertEqual(status, 403)

    def test_answer_carries_over_when_hazard_escalates_to_high_risk(self):
        self.record('person_entered_zone', 0)
        status, _ = self.request('POST', '/me/hazard/response',
                                 {'response': 'worker_moving_away'}, self.token('resp-op1'))
        self.assertEqual(status, 200)
        high = self.record('person_high_risk', 1000)
        self.assertEqual(high['status'], 'active')
        self.assertEqual(high['operatorResponse'], 'worker_moving_away')


if __name__ == '__main__':
    unittest.main()
