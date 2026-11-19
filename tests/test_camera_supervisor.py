"""Kernel supervisor kamera — intent desired_on + reconciliation loop.

Keputusan owner 14 Nov 2026 (arsitektur demo-ready): "start kamera" berubah
dari event sekali-tekan menjadi KONDISI yang dijamin. Kolom `cameras.desired_on`
menyimpan intent (start/stop HSE menulisnya), supervisor di CameraManager
memastikan runtime cocok: belum jalan → dinyalakan; frame beku → restart;
RTSP fallback-error → dinyalakan ulang dengan cadence lambat (produser telat
self-heal). Restart server tidak pernah lagi meninggalkan "online bohong".
"""
import os
import sys
import time
import unittest
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from Sources.cameras.service import CameraError


def _camera_row(factory, camera_id):
    with factory() as connection:
        return connection.execute(
            'SELECT status, desired_on, health_note FROM cameras WHERE id=%s',
            (camera_id,)).fetchone()


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1',
                     'Requires local PostgreSQL')
class CameraDesiredStateTests(unittest.TestCase):
    """Migrasi 017 + service: intent tersimpan dan terbaca."""

    @classmethod
    def setUpClass(cls):
        from Sources.infrastructure.database.connection import connect, database_url
        from Sources.infrastructure.database.migrate import migrate

        cls.admin = psycopg.connect('dbname=postgres host=/tmp', autocommit=True)
        name = 'safety_supv_test_' + uuid4().hex
        cls.admin.execute(sql.SQL('CREATE DATABASE {} OWNER safety_app').format(sql.Identifier(name)))
        cls.conninfo = make_conninfo(database_url(), dbname=name)
        cls.factory = staticmethod(lambda: connect(cls.conninfo))
        with cls.factory() as connection:
            migrate(connection)

        def stop_and_drop():
            cls.admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
            cls.admin.close()
        cls.addClassCleanup(stop_and_drop)

    def _camera(self, name='Cam Sup'):
        from Sources.cameras.service import create_camera
        return create_camera(name, 'Tes', 'rtsp', f'rtsp://127.0.0.1:8554/{uuid4().hex}',
                             None, False, int(time.time() * 1000),
                             connection_factory=self.factory)

    def test_migrasi_desired_on_default_true(self):
        """Migrasi 017: kolom desired_on ada; kamera baru desired (auto-start
        register konsisten)."""
        row = _camera_row(self.factory, self._camera()['id'])
        self.assertIs(row[1], True, 'desired_on default true (intent jalan)')

    def test_start_stop_menulis_intent(self):
        """set_desired = tulis intent; service membaca balik."""
        from Sources.cameras import service
        camera = self._camera('Cam Intent')
        cid = camera['id']
        service.set_desired(cid, False, connection_factory=self.factory)
        self.assertIs(_camera_row(self.factory, cid)[1], False)
        service.set_desired(cid, True, connection_factory=self.factory)
        self.assertIs(_camera_row(self.factory, cid)[1], True)
        listed = [c for c in service.list_cameras(connection_factory=self.factory)
                  if c['id'] == cid][0]
        self.assertIs(listed['desiredOn'], True, 'API list memuat desiredOn')


class _ScriptedReader:
    """Reader palsu dengan skenario per pemanggilan (indeks naik tiap open)."""

    def __init__(self, scenarios):
        self._scenarios = list(scenarios)
        self._i = -1

    def _current(self):
        self._i += 1 if self._i + 1 < len(self._scenarios) else 0
        return self._scenarios[min(self._i, len(self._scenarios) - 1)]

    @property
    def effective_fps(self):
        return 30.0

    def frames(self):
        return iter(self._current())

    def release(self):
        pass


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1',
                     'Requires local PostgreSQL')
class SupervisorReconcileTests(unittest.TestCase):
    """reconcile_once: runtime mengejar intent (tanpa jaringan — reader stub)."""

    @classmethod
    def setUpClass(cls):
        from Sources.infrastructure.database.connection import connect, database_url
        from Sources.infrastructure.database.migrate import migrate

        cls.admin = psycopg.connect('dbname=postgres host=/tmp', autocommit=True)
        name = 'safety_supv_recon_' + uuid4().hex
        cls.admin.execute(sql.SQL('CREATE DATABASE {} OWNER safety_app').format(sql.Identifier(name)))
        cls.conninfo = make_conninfo(database_url(), dbname=name)
        with connect(cls.conninfo) as connection:
            migrate(connection)
        from Sources.users.create import create_user
        cls.hse_id = create_user('supv-hse', 'HSE Supv', 'hse', 'password-demo-2026',
                                 lambda: connect(cls.conninfo))

        def stop_and_drop():
            cls.admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
            cls.admin.close()
        cls.addClassCleanup(stop_and_drop)

    def _manager_with(self, readers):
        """Manager di atas DB uji; _open_reader memakai stub per skenario."""
        from Sources.cameras.playback import CameraManager
        from Sources.infrastructure.database.connection import connect

        manager = CameraManager(connection_factory=lambda: connect(self.conninfo))
        script = _ScriptedReader(readers)
        self.calls = {'opens': 0}

        def _open_reader(camera):
            self.calls['opens'] += 1
            return script
        manager._open_reader = _open_reader
        return manager

    def _camera(self, name):
        from Sources.cameras.service import create_camera
        return create_camera(name, 'Tes', 'rtsp', f'rtsp://127.0.0.1:8554/{uuid4().hex}',
                              None, False, int(time.time() * 1000),
                              connection_factory=self.factory_like())

    def factory_like(self):
        from Sources.infrastructure.database.connection import connect
        return lambda: connect(self.conninfo)

    def _then_stop(self, manager):
        self.addCleanup(lambda: [manager.stop(cid) for cid in list(manager._threads)])

    @staticmethod
    def _endless_frames(frame):
        index = 0
        while True:
            yield (index, index * 33, frame)
            index += 1

    def test_desired_belum_jalan_dinyalakan(self):
        import cv2, numpy
        frame = numpy.zeros((16, 16, 3), dtype=numpy.uint8)
        manager = self._manager_with([self._endless_frames(frame)])
        camera = self._camera('Recon Start')
        cid = camera['id']
        self._then_stop(manager)
        manager.start_supervisor()
        try:
            deadline = time.time() + 6
            while time.time() < deadline and not manager.is_running(cid):
                time.sleep(0.2)
            self.assertTrue(manager.is_running(cid),
                            'kamera desired harus dinyalakan oleh supervisor')
        finally:
            manager.stop_supervisor()

    def test_intent_off_dihentikan(self):
        from Sources.cameras import service
        import cv2, numpy
        frame = numpy.zeros((16, 16, 3), dtype=numpy.uint8)
        manager = self._manager_with([self._endless_frames(frame)])
        camera = self._camera('Recon Stop')
        cid = camera['id']
        manager.start(camera)          # jalan manual dulu
        service.set_desired(cid, False, connection_factory=self.factory_like())
        manager.start_supervisor()
        try:
            deadline = time.time() + 8
            while time.time() < deadline and manager.is_running(cid):
                time.sleep(0.2)
        finally:
            manager.stop_supervisor()
        self.assertFalse(manager.is_running(cid),
                         'intent off → supervisor mematikan playback')

    def test_rtsp_error_retry_cadence(self):
        """Fallback error (produser mati) tidak dispam: tunggu jendela retry."""
        from Sources.cameras import service
        camera = self._camera('Recon Retry')
        cid = camera['id']
        manager = self._manager_with([[]])      # reader berakhir seketika = budget habis
        manager.start(camera)                     # → rtsp fallback error
        deadline = time.time() + 8
        with self.factory_like()() as connection:
            while time.time() < deadline:
                row = connection.execute('SELECT status FROM cameras WHERE id=%s',
                                         (cid,)).fetchone()
                if row[0] == 'error':
                    break
                time.sleep(0.2)
        self.assertEqual(_camera_row(self.factory_like(), cid)[0], 'error')
        # Cadence: retry_at tercatat di masa depan — tick berikut tak menyala.
        manager._retry_at[cid] = time.time() * 1000 + 10_000_000
        manager.reconcile_once()
        self.assertFalse(manager.is_running(cid),
                         'dalam jendela tenang, supervisor tidak boleh restart')


if __name__ == '__main__':
    unittest.main()
