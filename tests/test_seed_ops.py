"""Sesi demo seed 7 hari + relogin simulasi (keputusan owner 14 Nov 2026).

Dua perilaku baru:
1. `seed_operational_state` membuat sesi demo berumur DEFAULT 7 hari
   (override: env HARNES_SEED_SESSION_DAYS) — bukan lagi 12 jam — supaya
   gate "model jalan hanya saat unit operating" tidak diam-diam mati
   sehari setelah reset dev.
2. `Tools/relogin_demo.py` (menu harnes 're-login simulasi'): ulangi
   roleplay tiap operator ter-assignment lewat HTTP asli (login →
   PUT /me/shift → PUT /me/operation) — cache operating di server ikut
   segar tanpa restart server.

Isolasi: seed_demo memakai connect() modul default (baca env
DATABASE_URL) — tiap kelas test menimpa env DATABASE_URL ke DB scratch
lalu memulihkannya, supaya test TIDAK pernah menyentuh database dev.
"""
import http.client
import json
import os
import sys
import time
import unittest
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Tools'))

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from seed_demo import DEMO_PASSWORD


def _open_scratch(admin):
    """Buat DB scratch + alihkan env DATABASE_URL kesana.

    Return (dbname, conninfo_baru, env_lama) — pemanggil wajib memanggil
    _close_scratch() di cleanup supaya env dipulihkan.
    """
    from Sources.infrastructure.database.connection import database_url
    original_env = os.environ.get('DATABASE_URL')
    base = database_url()
    name = 'safety_seedops_test_' + uuid4().hex
    admin.execute(sql.SQL('CREATE DATABASE {} OWNER safety_app').format(sql.Identifier(name)))
    conninfo = make_conninfo(base, dbname=name)
    os.environ['DATABASE_URL'] = conninfo
    return name, conninfo, original_env


def _close_scratch(admin, name, original_env):
    os.environ.pop('DATABASE_URL', None)
    if original_env is not None:
        os.environ['DATABASE_URL'] = original_env
    admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
    admin.close()


def _connect_conninfo(conninfo):
    from Sources.infrastructure.database.connection import connect
    return connect(conninfo)


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1',
                     'Requires local PostgreSQL')
class SeedSessionDefaultSevenDayTests(unittest.TestCase):
    """DB scratch: kamera + budi → seed → sesi default ±7 hari."""

    @classmethod
    def setUpClass(cls):
        from Sources.infrastructure.database.migrate import migrate
        from Sources.users.create import create_user

        cls.admin = psycopg.connect('dbname=postgres host=/tmp', autocommit=True)
        cls.dbname, cls.conninfo, cls._env_old = _open_scratch(cls.admin)
        with _connect_conninfo(cls.conninfo) as connection:
            migrate(connection)
        cls.budi_id = create_user('budi', 'Budi', 'operator', DEMO_PASSWORD,
                                  lambda: _connect_conninfo(cls.conninfo))
        cls.addClassCleanup(
            lambda: _close_scratch(cls.admin, cls.dbname, cls._env_old))

    def test_sesi_seed_default_7_hari(self):
        """Default: expiry antara 6–8 hari (bukan 12 jam)."""
        import seed_demo
        from Sources.cameras.service import create_camera
        create_camera('Cam Seed', 'Tes', 'rtsp', f'rtsp://127.0.0.1:8554/{uuid4().hex}',
                      None, False, int(time.time() * 1000),
                      lambda: _connect_conninfo(self.conninfo))
        seed_demo.seed_equipment_instances()   # katalog id fallback (urutan seed dev)
        before = time.time()
        seed_demo.seed_operational_state()

        with _connect_conninfo(self.conninfo) as connection:
            row = connection.execute(
                "SELECT expires_at FROM sessions WHERE user_id=%s "
                "ORDER BY expires_at DESC LIMIT 1", (self.budi_id,)).fetchone()
            binding = connection.execute(
                "SELECT operation_started_at_ms FROM shift_bindings WHERE operator_id=%s",
                (self.budi_id,)).fetchone()
        self.assertIsNotNone(row, 'sesi demo harus dibuat')
        days = (row[0].timestamp() - before) / 86_400
        self.assertGreater(days, 6, 'sesi demo harus ±7 hari default')
        self.assertLess(days, 8)
        self.assertIsNotNone(binding[0], 'binding harus Start Operating')


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1',
                     'Requires local PostgreSQL')
class SeedSessionEnvOverrideTests(unittest.TestCase):
    """DB scratch: kamera + sari → HARNES_SEED_SESSION_DAYS=2 → sesi ±2 hari."""

    @classmethod
    def setUpClass(cls):
        from Sources.infrastructure.database.migrate import migrate
        from Sources.users.create import create_user

        cls.admin = psycopg.connect('dbname=postgres host=/tmp', autocommit=True)
        cls.dbname, cls.conninfo, cls._env_old = _open_scratch(cls.admin)
        with _connect_conninfo(cls.conninfo) as connection:
            migrate(connection)
        create_user('sari', 'Sari', 'operator', DEMO_PASSWORD,
                    lambda: _connect_conninfo(cls.conninfo))
        cls.addClassCleanup(
            lambda: _close_scratch(cls.admin, cls.dbname, cls._env_old))

    def test_override_env_mengubah_umur_sesi(self):
        """HARNES_SEED_SESSION_DAYS=2 → sesi pasangan baru sari ±2 hari."""
        import seed_demo
        from Sources.cameras.service import create_camera
        create_camera('Cam Sari', 'Tes', 'rtsp', f'rtsp://127.0.0.1:8554/{uuid4().hex}',
                      None, False, int(time.time() * 1000),
                      lambda: _connect_conninfo(self.conninfo))

        with _connect_conninfo(self.conninfo) as connection:
            sari = connection.execute(
                "SELECT id FROM users WHERE username='sari'").fetchone()[0]

        old = os.environ.get('HARNES_SEED_SESSION_DAYS')
        os.environ['HARNES_SEED_SESSION_DAYS'] = '2'
        try:
            before = time.time()
            seed_demo.seed_equipment_instances()
            seed_demo.seed_operational_state()
        finally:
            if old is None:
                os.environ.pop('HARNES_SEED_SESSION_DAYS', None)
            else:
                os.environ['HARNES_SEED_SESSION_DAYS'] = old

        with _connect_conninfo(self.conninfo) as connection:
            row = connection.execute(
                "SELECT expires_at FROM sessions WHERE user_id=%s "
                "ORDER BY expires_at DESC LIMIT 1", (sari,)).fetchone()
        self.assertIsNotNone(row, 'sesi sari harus dibuat oleh seed')
        days = (row[0].timestamp() - before) / 86_400
        self.assertGreater(days, 1.5)
        self.assertLess(days, 2.5)


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1',
                     'Requires local PostgreSQL + AlertServer')
class ReloginSimulationTests(unittest.TestCase):
    """Sesi habis → status idle; relogin → operating TRUE tanpa restart."""

    @classmethod
    def setUpClass(cls):
        from Sources.alerts.bus import EventBus
        from Sources.alerts.server import AlertServer
        from Sources.alerts.store import AlertStore
        from Sources.api.context import AppContext
        from Sources.assignments.service import create_assignment
        from Sources.auth.limits import LoginLimits
        from Sources.auth.service import AuthService
        from Sources.cameras.service import create_camera
        from Sources.core.evaluator import RiskEvaluator
        from Sources.equipment import registry
        from Sources.equipment.operation import OperationStatus
        from Sources.infrastructure.database.migrate import migrate
        from Sources.users.create import create_user
        import threading

        cls.admin = psycopg.connect('dbname=postgres host=/tmp', autocommit=True)
        cls.dbname, cls.conninfo, cls._env_old = _open_scratch(cls.admin)
        factory = staticmethod(lambda: _connect_conninfo(cls.conninfo))
        with factory.__func__() as connection:
            migrate(connection)
        hse_id = create_user('relogin-hse', 'HSE', 'hse', DEMO_PASSWORD, factory.__func__)
        op_id = create_user('relogin-op', 'Op', 'operator', DEMO_PASSWORD, factory.__func__)
        camera = create_camera('Cam Relogin', 'Tes', 'rtsp',
                               f'rtsp://127.0.0.1:8554/{uuid4().hex}', None, False,
                               int(time.time() * 1000), factory.__func__)
        cls.unit = registry.create_from_camera(camera['id'], 'Unit Relogin',
                                               hse_id, int(time.time() * 1000),
                                               connection_factory=factory.__func__)
        create_assignment(cls.unit['equipmentInstanceId'], op_id,
                          int(time.time() * 1000), connection_factory=factory.__func__)
        # Tanpa seed_operational_state: tidak ada sesi/binding → operating mati.

        cls.server = AlertServer(('127.0.0.1', 0), context=AppContext(
            auth=AuthService(factory.__func__), login_limits=LoginLimits(),
            risk=RiskEvaluator(), detector=None, bus=EventBus(),
            connect=factory.__func__, store=AlertStore(factory.__func__),
            operation_status=OperationStatus(factory.__func__)))
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

        def stop_and_drop():
            cls.server.shutdown()
            cls.server.server_close()
            cls.worker.join(timeout=5)
            _close_scratch(cls.admin, cls.dbname, cls._env_old)
        cls.addClassCleanup(stop_and_drop)

    def _token(self, username):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        body = json.dumps({'username': username, 'password': DEMO_PASSWORD})
        connection.request('POST', '/auth/login', body, {'Content-Type': 'application/json'})
        data = json.loads(connection.getresponse().read())
        connection.close()
        return data['access_token']

    def _operation_active(self):
        token = self._token('relogin-hse')
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        connection.request('GET', '/equipment/status',
                            headers={'Authorization': 'Bearer ' + token})
        items = json.loads(connection.getresponse().read())['items']
        connection.close()
        return bool(items) and items[0].get('operationActive')

    def test_relogin_hidupkan_operating_dan_cache_server(self):
        """Sesi habis → /equipment/status idle; relogin → operating TRUE
        tanpa restart server."""
        import relogin_demo
        self.assertFalse(self._operation_active(),
                         'pra-kondisi: unit belum operating (tanpa sesi)')

        results = relogin_demo.relogin(
            f'http://{self.server.server_address[0]}:{self.server.server_address[1]}',
            connection_factory=lambda: _connect_conninfo(self.conninfo))

        self.assertEqual(len(results), 1, 'satu operator ter-assignment')
        step = results[0]
        self.assertTrue(all(step.get(k) for k in ('login', 'shift', 'operating')),
                        f'semua langkah harus sukses: {step}')
        self.assertTrue(self._operation_active(),
                        'setelah relogin: operationActive harus TRUE via API')


if __name__ == '__main__':
    unittest.main()
