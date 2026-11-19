"""Integration test kelola akun oleh HSE (api-draft 4.2 N2-B; DB-gated).

Alur penuh lewat HTTP: HSE membuat akun, reset password (sesi target dicabut),
ubah displayName / active. Nonaktifkan = soft-disable: is_active memblokir
otentikasi (jalan via AuthService) DAN device_tokens + shift_bindings target
dibersihkan agar push berhenti. devicesOnline dihitung dari device_tokens
(jalur /me/device), bukan tabel devices lama yang CRUD-nya diparkir.
"""
import http.client
import json
import os
import sys
import time
import unittest
from pathlib import Path
from threading import Thread
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from Sources.auth.limits import LoginLimits
from Sources.auth.service import Unauthorized

PASSWORD = 'temporary-test-password'


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1',
                     'Requires local PostgreSQL')
class UserAdminHTTPTests(unittest.TestCase):
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
        name = 'safety_users_test_' + uuid4().hex
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
        # Katalog equipment_instance (skej tim 9724576): beberapa test menset
        # assignment ke id contoh — daftarkan sekali per db uji.
        with self.factory() as connection:
            for equipment in ('excavator-unit-1', 'unit-03', 'unit-04'):
                connection.execute(
                    "INSERT INTO equipment_instances(equipment_instance_id, name) "
                    "VALUES (%s, 'Test Excavator') ON CONFLICT DO NOTHING", (equipment,))
        # Rate-limit login per-IP baru tiap test — beberapa test login berkali-kali.
        self.server.context.login_limits = LoginLimits()

    def request(self, method, path, body=None, token=None, raw=False):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=20)
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        connection.request(method, path,
                           json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        if raw:
            return response.status, data, dict(response.getheaders())
        if data and 'json' in response.getheader('Content-Type', ''):
            return response.status, json.loads(data)
        # Bukan JSON (mis. 501 dari BaseHTTPRequestHandler saat route belum ada).
        return response.status, data.decode('utf-8', 'replace')

    def token(self, username, password=PASSWORD):
        return self.server.context.auth.login(username, password)['access_token']

    def _create_operator(self, token, username=None, password='rahasia-baru-123'):
        """Akun frash per test — DB throwaway dipakai bersama antar method."""
        username = username or f'budi-{uuid4().hex[:8]}'
        status, account = self.request(
            'POST', '/users',
            {'username': username, 'displayName': username.title(),
             'role': 'operator', 'password': password}, token=token)
        assert status == 201, account
        return account

    # -- POST /users ---------------------------------------------------------

    def test_hse_creates_operator_account_over_http(self):
        hse = self.token('admin-hse')
        username = f'budi-{uuid4().hex[:8]}'
        status, account = self.request('POST', '/users',
                                       {'username': username, 'displayName': 'Budi Santoso',
                                        'role': 'operator', 'password': 'rahasia-baru-123'},
                                       token=hse)
        self.assertEqual(status, 201)
        self.assertEqual(account['username'], username)
        self.assertEqual(account['displayName'], 'Budi Santoso')
        self.assertEqual(account['role'], 'operator')
        self.assertTrue(account['active'])
        self.assertEqual(account['activeAssignments'], 0)
        self.assertEqual(account['devicesOnline'], 0)
        self.assertNotIn('password', account)
        self.assertNotIn('passwordHash', account)
        # Akun baru langsung bisa dipakai login.
        self.assertIsNotNone(self.token(username, 'rahasia-baru-123'))

    def test_create_account_access_and_validation(self):
        hse = self.token('admin-hse')
        operator = self.token('op-worker')
        base = f'budi-{uuid4().hex[:8]}'  # pasti mengandung huruf kecil
        body = {'username': base, 'displayName': 'Budi S.',
                'role': 'operator', 'password': 'rahasia-baru-123'}
        # Operator tidak boleh membuat akun; tanpa sesi juga ditolak.
        self.assertEqual(self.request('POST', '/users', body, token=operator)[0], 403)
        self.assertEqual(self.request('POST', '/users', body)[0], 401)
        # Validasi bentuk: role, username, password (12-256), displayName.
        self.assertEqual(self.request('POST', '/users',
                     {**body, 'username': 'ok-user', 'role': 'admin'}, token=hse)[0], 400)
        self.assertEqual(self.request('POST', '/users',
                     {**body, 'username': '!invalid'}, token=hse)[0], 400)
        self.assertEqual(self.request('POST', '/users',
                     {**body, 'username': 'ok-user', 'password': 'pendek'}, token=hse)[0], 400)
        self.assertEqual(self.request('POST', '/users',
                     {**body, 'username': 'ok-user', 'displayName': '  '}, token=hse)[0], 400)
        # Username unik case-insensitive (unique index lower(username)) → 409.
        status, first = self.request('POST', '/users',
                                     {**body, 'username': base}, token=hse)
        self.assertEqual(status, 201)
        status, payload = self.request('POST', '/users',
                                       {**body, 'username': base.upper()}, token=hse)
        self.assertEqual((status, payload['error']['code']), (409, 'already_exists'))

    # -- PUT /users/{id}/password --------------------------------------------

    def test_password_reset_updates_and_revokes_sessions(self):
        hse = self.token('admin-hse')
        account = self._create_operator(hse)
        old_token = self.token(account['username'], 'rahasia-baru-123')
        self.assertEqual(self.request('GET', '/auth/me', token=old_token)[0], 200)

        status, payload = self.request('PUT', f"/users/{account['id']}/password",
                                       {'password': 'password-baru-456'}, token=hse)
        self.assertEqual(status, 200)
        self.assertEqual(payload['username'], account['username'])
        self.assertNotIn('password', payload)
        # Sesi lama operator dicabut; login hanya dengan password baru.
        self.assertEqual(self.request('GET', '/auth/me', token=old_token)[0], 401)
        with self.assertRaises(Unauthorized):
            self.token(account['username'], 'rahasia-baru-123')
        self.assertIsNotNone(self.token(account['username'], 'password-baru-456'))

        # User tidak dikenal → 404; input tidak valid → 400.
        self.assertEqual(self.request('PUT', f'/users/{uuid4()}/password',
                     {'password': 'password-baru-456'}, token=hse)[0], 404)
        self.assertEqual(self.request('PUT', '/users/bukan-uuid/password',
                     {'password': 'password-baru-456'}, token=hse)[0], 400)
        self.assertEqual(self.request('PUT', f"/users/{account['id']}/password",
                     {'password': 'pendek'}, token=hse)[0], 400)

    # -- PATCH /users/{id} -----------------------------------------------------

    def test_patch_updates_display_name(self):
        hse = self.token('admin-hse')
        account = self._create_operator(hse)
        status, payload = self.request('PATCH', f"/users/{account['id']}",
                                       {'displayName': 'Budi Santoso'}, token=hse)
        self.assertEqual(status, 200)
        self.assertEqual(payload['displayName'], 'Budi Santoso')
        self.assertTrue(payload['active'])
        # Terlihat di direktori.
        _, directory = self.request('GET', '/users', token=hse)
        item = next(item for item in directory['items'] if item['username'] == account['username'])
        self.assertEqual(item['displayName'], 'Budi Santoso')
        # PATCH tanpa field yang dikenal → 400; user tidak dikenal → 404;
        # tipe active bukan bool → 400.
        self.assertEqual(self.request('PATCH', f"/users/{account['id']}", {},
                                      token=hse)[0], 400)
        self.assertEqual(self.request('PATCH', f"/users/{uuid4()}",
                     {'displayName': 'Siapa'}, token=hse)[0], 404)
        self.assertEqual(self.request('PATCH', f"/users/{account['id']}",
                     {'active': 'yes'}, token=hse)[0], 400)

    def test_deactivate_is_soft_disable_and_purges_push_routes(self):
        hse = self.token('admin-hse')
        account = self._create_operator(hse)
        operator_id = account['id']
        op_token = self.token(account['username'], 'rahasia-baru-123')
        # HSE assignment authorizes the operator's device + shift binding.
        self.assertEqual(self.request('POST', '/assignments', {
            'equipmentInstanceId': 'excavator-unit-1', 'operatorId': operator_id,
        }, token=hse)[0], 201)
        self.assertEqual(self.request('POST', '/me/device',
                     {'pushToken': 'a' * 64, 'model': 'iPhone 17', 'osVersion': '27.0',
                      'appVersion': '1.0'}, token=op_token)[0], 201)
        self.assertEqual(self.request('PUT', '/me/shift',
                     {'onShiftFor': ['excavator-unit-1']}, token=op_token)[0], 200)

        status, payload = self.request('PATCH', f'/users/{operator_id}',
                                       {'active': False}, token=hse)
        self.assertEqual(status, 200)
        self.assertFalse(payload['active'])
        # is_active memblokir sesi & login baru (AuthService, tanpa kerja ekstra).
        self.assertEqual(self.request('GET', '/auth/me', token=op_token)[0], 401)
        with self.assertRaises(Unauthorized):
            self.token(account['username'], 'rahasia-baru-123')
        # Push routes bersih: device_tokens + shift_bindings kosong, routing None.
        with self.factory() as connection:
            tokens = connection.execute(
                'SELECT count(*) FROM device_tokens WHERE operator_id=%s', (operator_id,)).fetchone()[0]
            bindings = connection.execute(
                'SELECT count(*) FROM shift_bindings WHERE operator_id=%s', (operator_id,)).fetchone()[0]
        self.assertEqual((tokens, bindings), (0, 0))
        with self.factory() as connection:
            active_assignments = connection.execute(
                'SELECT count(*) FROM assignments WHERE operator_id=%s AND status=%s',
                (operator_id, 'active')).fetchone()[0]
        self.assertEqual(active_assignments, 0)
        from Sources.alerts.store import resolve_binding_operator
        self.assertIsNone(resolve_binding_operator('excavator-unit-1',
                                                  connection_factory=self.factory))
        # Aktivasi kembali menghidupkan login (soft-disable = reversibel).
        status, payload = self.request('PATCH', f'/users/{operator_id}',
                                       {'active': True}, token=hse)
        self.assertEqual((status, payload['active']), (200, True))
        self.assertIsNotNone(self.token(account['username'], 'rahasia-baru-123'))

    def test_assignment_authorizes_operator_shift_and_survives_logout(self):
        hse = self.token('admin-hse')
        account = self._create_operator(hse)
        operator = self.token(account['username'], 'rahasia-baru-123')

        status, assignment = self.request('POST', '/assignments', {
            'equipmentInstanceId': 'unit-03', 'operatorId': account['id'],
        }, token=hse)
        self.assertEqual(status, 201)
        self.assertFalse(assignment['shiftActive'])

        # Satu operator tidak boleh memegang dua unit aktif sekaligus.
        status, conflict = self.request('POST', '/assignments', {
            'equipmentInstanceId': 'unit-04', 'operatorId': account['id'],
        }, token=hse)
        self.assertEqual((status, conflict['error']['code']),
                         (409, 'operator_already_assigned'))

        status, mine = self.request('GET', '/me/assignment', token=operator)
        self.assertEqual(status, 200)
        self.assertEqual(mine['assignment']['equipmentInstanceId'], 'unit-03')

        status, payload = self.request('PUT', '/me/shift', {
            'onShiftFor': ['unit-99'],
        }, token=operator)
        self.assertEqual((status, payload['error']['code']),
                         (409, 'assignment_mismatch'))

        self.assertEqual(self.request('PUT', '/me/shift', {
            'onShiftFor': ['unit-03'],
        }, token=operator)[0], 200)
        # Semantik tim 12 Nov (016): binding = kehadiran operator; operating
        # baru menyala lewat aksi eksplisit /me/operation.
        self.assertEqual(self.request('PUT', '/me/operation', {
            'active': True,
        }, token=operator)[0], 200)
        _, listing = self.request('GET', '/assignments', token=hse)
        current = next(item for item in listing['items'] if item['id'] == assignment['id'])
        self.assertTrue(current['sessionActive'])
        self.assertTrue(current['shiftActive'])

        # Guard tim 12 Nov: operator yang unit-nya masih OPERATING ditolak
        # logout (409 unit_operating) — tikam stop dulu supaya bahaya tak
        # ditinggal tanpa pengawasan.
        status_guard, guard = self.request('POST', '/auth/logout', token=operator)
        self.assertEqual((status_guard, guard['error']['code']),
                         (409, 'unit_operating'))
        self.assertEqual(self.request('PUT', '/me/operation', {
            'active': False,
        }, token=operator)[0], 200)
        self.assertEqual(self.request('POST', '/auth/logout', token=operator)[0], 200)
        _, listing = self.request('GET', '/assignments', token=hse)
        current = next(item for item in listing['items'] if item['id'] == assignment['id'])
        self.assertFalse(current['sessionActive'])
        self.assertFalse(current['shiftActive'])

    def test_self_deactivation_is_forbidden(self):
        hse = self.token('admin-hse')
        _, me = self.request('GET', '/auth/me', token=hse)
        status, payload = self.request('PATCH', f"/users/{me['user']['id']}",
                                       {'active': False}, token=hse)
        self.assertEqual(status, 400)
        self.assertEqual(payload['error']['code'], 'self_deactivate_forbidden')
        # HSE tetap terautentikasi; idempotent re-assert active=true boleh.
        self.assertEqual(self.request('GET', '/auth/me', token=hse)[0], 200)
        status, payload = self.request('PATCH', f"/users/{me['user']['id']}",
                                       {'active': True}, token=hse)
        self.assertEqual(status, 200)

    # -- devicesOnline: sumber = device_tokens + presence stream ----------------

    def test_devices_online_counts_device_tokens_presence(self):
        hse = self.token('admin-hse')
        account = self._create_operator(hse)
        operator_id = account['id']
        op_token = self.token(account['username'], 'rahasia-baru-123')

        # Stream SSE hidup = operator online (presence bus).
        stream = http.client.HTTPConnection(*self.server.server_address, timeout=20)
        stream.request('GET', '/events', headers={'Authorization': 'Bearer ' + op_token})
        response = stream.getresponse()
        self.addCleanup(stream.close)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.readline(), b': connected\n')

        # Satu device terdaftar via /me/device; row di tabel devices lama diabaikan.
        self.assertEqual(self.request('POST', '/me/device',
                     {'pushToken': 'b' * 64, 'model': 'iPhone 17', 'osVersion': '27.0',
                      'appVersion': '1.0'}, token=op_token)[0], 201)
        with self.factory() as connection:
            connection.execute(
                'INSERT INTO devices(operator_id, model, os_version, app_version, '
                'registered_at_ms) VALUES (%s, %s, %s, %s, %s)',
                (operator_id, 'iPhone 16', '26.0', '0.9', 1))
        _, directory = self.request('GET', '/users', token=hse)
        item = next(item for item in directory['items'] if item['username'] == account['username'])
        self.assertEqual(item['devicesOnline'], 1)

        # Stream ditutup → presence harus hilang. Recheck auth di stream_events
        # terjadi saat listener mendapat event/heartbeat; event broadcast HSE
        # tidak dikirim ke stream operator (routing by role) — jadi kirim nudge
        # ROUTED ke operator ini + logout supaya recheck berikutnya langsung
        # memutus stream (deterministik, tanpa menunggu heartbeat 10 detik).
        stream.close()
        self.assertEqual(self.request('POST', '/auth/logout', token=op_token)[0], 200)
        self.server.publish({'message': 'presence-drop-nudge'},
                            route_operator_id=account['id'])
        deadline = time.monotonic() + 5
        item = None
        while time.monotonic() < deadline:
            _, directory = self.request('GET', '/users', token=hse)
            item = next(item for item in directory['items']
                        if item['username'] == account['username'])
            if item['devicesOnline'] == 0:
                break
            time.sleep(0.05)
        self.assertEqual(item['devicesOnline'], 0)


if __name__ == '__main__':
    unittest.main()
