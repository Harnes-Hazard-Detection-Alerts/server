"""Integration test assignment excavator<->operator (api-draft 4.5; DB-gated).

Contract upstream (origin/dev): HSE menugaskan via POST /assignments, operator
membaca satu penugasan aktifnya lewat GET /me/assignment (envelope
{"assignment": …|null}); DELETE /assignments/{id} = unassign soft-state
'ended' alasan unassigned_by_hse; satu operator & satu equipment masing-masing
hanya boleh memegang satu assignment aktif (409). Login operator eksklusif —
login baru memutus sesi lama — maka helper token di-CACHE per user: login
berulang dalam satu test akan membatalkan token yang sedang dipakai.
"""
import http.client
import json
import os
import sys
import unittest
from pathlib import Path
from threading import Thread
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from Sources.auth.limits import LoginLimits

PASSWORD = 'temporary-test-password'


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1',
                     'Requires local PostgreSQL')
class AssignmentHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from Sources.alerts.bus import EventBus
        from Sources.alerts.server import AlertServer
        from Sources.alerts.store import AlertStore
        from Sources.api.context import AppContext
        from Sources.auth.service import AuthService
        from Sources.core.evaluator import RiskEvaluator
        from Sources.infrastructure.database.connection import connect, database_url
        from Sources.infrastructure.database.migrate import migrate
        from Sources.users.create import create_user

        cls.admin = psycopg.connect('dbname=postgres host=/tmp', autocommit=True)
        name = 'safety_assignments_test_' + uuid4().hex
        cls.admin.execute(sql.SQL('CREATE DATABASE {} OWNER safety_app').format(sql.Identifier(name)))

        def factory():
            return connect(make_conninfo(database_url(), dbname=name))
        cls.factory = staticmethod(factory)
        with factory() as connection:
            migrate(connection)
        create_user('admin-hse', 'HSE Admin', 'hse', PASSWORD, factory)
        create_user('op-worker', 'Op Worker', 'operator', PASSWORD, factory)

        cls.server = AlertServer(('127.0.0.1', 0), context=AppContext(
            auth=AuthService(factory), login_limits=LoginLimits(),
            risk=RiskEvaluator(), detector=None, bus=EventBus(),
            connect=factory, store=AlertStore(factory)))
        cls.worker = Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

        def stop_and_drop():
            cls.server.shutdown()
            cls.server.server_close()
            cls.worker.join(timeout=5)
            cls.admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
            cls.admin.close()
        cls.addClassCleanup(stop_and_drop)

    def setUp(self):
        # Katalog equipment_instance (skej 9724576): id statis tes perlu
        # terdaftar sebelum POST /assignments; _assign mendaftarkan id dinamis.
        with self.factory() as connection:
            for equipment in ('unit-x', 'excavator-free', 'excavator-other'):
                connection.execute(
                    "INSERT INTO equipment_instances(equipment_instance_id, name) "
                    "VALUES (%s, 'Test Excavator') ON CONFLICT DO NOTHING", (equipment,))
        self.server.context.login_limits = LoginLimits()
        self._tokens = {}

    def tearDown(self):
        # Test berikutnya tidak boleh mewarisi assignment aktif — operator dan
        # equipment sama-sama unik-aktif, sisa state bikin 409 palsu.
        hse = self.token('admin-hse')
        _, listing = self.request('GET', '/assignments', token=hse)
        for item in listing['items']:
            if item['status'] == 'active':
                self.request('DELETE', f"/assignments/{item['id']}", token=hse)

    def request(self, method, path, body=None, token=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=20)
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        connection.request(method, path,
                           json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        if data and 'json' in response.getheader('Content-Type', ''):
            return response.status, json.loads(data)
        return response.status, data.decode('utf-8', 'replace')

    def token(self, username, password=PASSWORD):
        # Login operator eksklusif: login kedua memutus token pertama, jadi
        # sekali per user per test (cache). HSE tidak terpengaruh.
        if username not in self._tokens:
            self._tokens[username] = self.server.context.auth.login(
                username, password)['access_token']
        return self._tokens[username]

    def operator_id(self, username='op-worker'):
        return self.server.context.auth.authenticate(self.token(username))['id']

    def _assign(self, hse, equipment=None, operator_id=None):
        # Kontrak tim 12 Nov (9724576): assignment mensyaratkan equipment
        # terdaftar di katalog equipment_instances — daftarkan bila belum.
        operator_id = operator_id or self.operator_id()
        equipment = equipment or f'unit-{uuid4().hex[:8]}'
        with self.factory() as connection:
            connection.execute(
                "INSERT INTO equipment_instances(equipment_instance_id, name) "
                "VALUES (%s, 'Test Excavator') ON CONFLICT DO NOTHING", (equipment,))
        return self.request('POST', '/assignments',
                            {'equipmentInstanceId': equipment,
                             'operatorId': operator_id}, token=hse)

    def test_create_read_and_unassign_full_cycle(self):
        hse = self.token('admin-hse')
        op = self.token('op-worker')
        operator_id = self.operator_id()
        equipment = f'unit-{uuid4().hex[:8]}'

        status, item = self._assign(hse, equipment=equipment)
        self.assertEqual(status, 201)
        self.assertEqual(item['equipmentInstanceId'], equipment)
        self.assertEqual(item['operatorId'], operator_id)
        self.assertEqual(item['status'], 'active')
        self.assertFalse(item['shiftActive'])  # belum ada binding runtime

        # Operator membaca penugasannya sendiri; HSE tidak punya endpoint itu.
        status, mine = self.request('GET', '/me/assignment', token=op)
        self.assertEqual(status, 200)
        self.assertEqual(mine['assignment']['id'], item['id'])
        self.assertEqual(mine['assignment']['equipmentInstanceId'], equipment)
        self.assertEqual(self.request('GET', '/me/assignment', token=hse)[0], 403)

        # Direktori HSE menghitung assignment aktif per operator.
        status, users = self.request('GET', '/users', token=hse)
        self.assertEqual([u['activeAssignments'] for u in users['items']
                          if u['id'] == operator_id], [1])

        # Unassign = soft-state 'ended'; baris tetap untuk audit.
        status, ended = self.request('DELETE', f"/assignments/{item['id']}", token=hse)
        self.assertEqual(status, 200)
        self.assertEqual(ended['status'], 'ended')
        self.assertEqual(ended['endedReason'], 'unassigned_by_hse')
        _, listing = self.request('GET', '/assignments?status=ended', token=hse)
        row = next(a for a in listing['items'] if a['id'] == item['id'])
        self.assertEqual(row['status'], 'ended')
        self.assertEqual(row['endedReason'], 'unassigned_by_hse')

        status, mine = self.request('GET', '/me/assignment', token=op)
        self.assertIsNone(mine['assignment'])

        # Equipment dan operator bebas; unit yang sama bisa ditunjuk ulang.
        status, again = self._assign(hse, equipment=equipment)
        self.assertEqual(status, 201)
        self.assertEqual(again['status'], 'active')

    def test_access_control_and_validation(self):
        hse = self.token('admin-hse')
        op = self.token('op-worker')
        # Role salah ditolak di semua jalur assignment; body kosong → 400.
        self.assertEqual(self._assign(op)[0], 403)
        self.assertEqual(
            self.request('POST', '/assignments',
                         {'equipmentInstanceId': 'unit-x', 'operatorId': 'no'},
                         token=hse)[0], 400)
        self.assertEqual(
            self.request('GET', '/me/assignment', token=hse)[0], 403)
        self.assertEqual(
            self.request('DELETE', f'/assignments/{uuid4()}', token=op)[0], 403)

    def test_domain_errors(self):
        hse = self.token('admin-hse')
        hse_id = self.server.context.auth.authenticate(hse)['id']
        # Operator tidak dikenal → 404; role bukan operator → 422.
        self.assertEqual(
            self.request('POST', '/assignments',
                         {'equipmentInstanceId': 'unit-x', 'operatorId': str(uuid4())},
                         token=hse)[0], 404)
        status, payload = self.request(
            'POST', '/assignments',
            {'equipmentInstanceId': 'unit-x', 'operatorId': hse_id}, token=hse)
        self.assertEqual((status, payload['error']['code']), (422, 'operator_inactive'))
        # Konteks upstream: POST sama persis idempoten (200); PINDAH operator
        # pada equipment sama = transfer (201, lama berakhir reassigned_by_hse);
        # satu operator dua equipment = 409 operator_already_assigned.
        unit_a = f'unit-a-{uuid4().hex[:8]}'
        status, first = self._assign(hse, equipment=unit_a)
        self.assertEqual(status, 201)
        status, same = self._assign(hse, equipment=unit_a)
        self.assertEqual((status, same['id']), (200, first['id']))

        other = self._create_operator(hse)
        status, moved = self.request(
            'POST', '/assignments',
            {'equipmentInstanceId': unit_a, 'operatorId': other['id']}, token=hse)
        self.assertEqual(status, 201)
        _, listing = self.request('GET', '/assignments?status=ended', token=hse)
        row = next(a for a in listing['items'] if a['id'] == first['id'])
        self.assertEqual((row['status'], row['endedReason']),
                         ('ended', 'reassigned_by_hse'))

        unit_b = f'unit-b-{uuid4().hex[:8]}'
        status, payload = self._assign(hse, equipment=unit_b, operator_id=other['id'])
        self.assertEqual(
            (status, payload['error']['code']), (409, 'operator_already_assigned'))
        # Unassign id tak dikenal → 404.
        self.assertEqual(self.request(
            'DELETE', f"/assignments/{uuid4()}", token=hse)[0], 404)

    def test_deactivate_operator_ends_assignment_and_frees_equipment(self):
        hse = self.token('admin-hse')
        account = self._create_operator(hse)
        status, item = self.request(
            'POST', '/assignments',
            {'equipmentInstanceId': 'excavator-free', 'operatorId': account['id']},
            token=hse)
        self.assertEqual(status, 201)

        status, payload = self.request('PATCH', f"/users/{account['id']}",
                                       {'active': False}, token=hse)
        self.assertEqual(status, 200)

        with self.factory() as connection:
            rows = connection.execute(
                'SELECT status, ended_reason FROM assignments '
                'WHERE id=%s', (item['id'],)).fetchall()
        self.assertEqual(rows, [('ended', 'operator_deactivated')])

        # Equipment bebas: operator live boleh ditunjuk ke unit yang sama.
        status, payload = self.request(
            'POST', '/assignments',
            {'equipmentInstanceId': 'excavator-free',
             'operatorId': self.operator_id()},
            token=hse)
        self.assertEqual(status, 201)

        # Penugasan ke operator nonaktif ditolak 422.
        status, payload = self.request(
            'POST', '/assignments',
            {'equipmentInstanceId': 'excavator-other', 'operatorId': account['id']},
            token=hse)
        self.assertEqual((status, payload['error']['code']), (422, 'operator_inactive'))

    def _create_operator(self, token, username=None, password='rahasia-baru-123'):
        username = username or f'budi-{uuid4().hex[:8]}'
        status, account = self.request(
            'POST', '/users',
            {'username': username, 'displayName': username.title(),
             'role': 'operator', 'password': password}, token=token)
        self.assertEqual(status, 201)
        return account


if __name__ == '__main__':
    unittest.main()
