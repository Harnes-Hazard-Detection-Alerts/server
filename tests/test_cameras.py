"""Integration test modul kamera (DB-gated, perlu cv2 utk video sintetis).

Alur penuh lewat HTTP: daftar kamera dari file video sintetis, start playback
(realtime per fps), snapshot JPEG (ETag, raw/annotated) saat frame mengalir,
stop, delete. Kamera rtsp live: start 200, reconnect maksimal 3x open gagal
beruntun lalu scan berhenti — fallback status error (produser mati
dibiarkan terdaftar; pemulihan lewat start ulang), validasi URL.
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

HAS_CV2 = False
try:
    import cv2
    import numpy  # noqa: F401 — cv2 imencode di manager memerlukan numpy array
    HAS_CV2 = True
except ImportError:
    pass


def _synthetic_video(path, frames=24, fps=12.0, size=(320, 240)):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'mp4v'), fps, size)
    for index in range(frames):
        frame = numpy.zeros((size[1], size[0], 3), dtype=numpy.uint8)
        frame[:, : , 0] = (index * 10) % 255  # isi biru berubah per frame
        writer.write(frame)
    writer.release()


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1' and HAS_CV2,
                     'Requires local PostgreSQL + cv2 (requirements-ml)')
class CameraHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from Sources.alerts.store import AlertStore
        from Sources.alerts.server import AlertServer
        from Sources.alerts.bus import EventBus
        from Sources.api.context import AppContext
        from Sources.auth.limits import LoginLimits
        from Sources.auth.service import AuthService
        from Sources.core.evaluator import RiskEvaluator
        from Sources.detection.model import DetectionModel
        from Sources.infrastructure.database.connection import connect, database_url
        from Sources.infrastructure.database.migrate import migrate
        from Sources.users.create import create_user

        cls.admin = psycopg.connect('dbname=postgres host=/tmp', autocommit=True)
        name = 'safety_cam_test_' + uuid4().hex
        cls.admin.execute(sql.SQL('CREATE DATABASE {} OWNER safety_app').format(sql.Identifier(name)))

        def factory():
            return connect(make_conninfo(database_url(), dbname=name))
        cls.factory = staticmethod(factory)
        with factory() as connection:
            migrate(connection)
        password = 'temporary-test-password'
        create_user('cam-hse', 'HSE cam', 'hse', password, factory)
        create_user('cam-op', 'Op cam', 'operator', password, factory)

        cls.video = Path('/var/folders/l1/lfbhk79d7lv0h6yqy9ydc4k00000gn/T/opencode/') / f'cam-test-{uuid4().hex}.mp4'
        _synthetic_video(cls.video)

        cls.server = AlertServer(('127.0.0.1', 0), context=AppContext(
            auth=AuthService(factory), login_limits=LoginLimits(),
            risk=RiskEvaluator(), detector=DetectionModel(),
            bus=EventBus(), connect=factory, store=AlertStore(factory)))
        cls.worker = Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

        def stop_and_drop():
            # Stop cameras SEBELUM DB dijatuhkan (thread playback menulis status).
            cameras = cls.server.context.cameras
            with cameras._lock:
                ids = list(cameras._threads)
            for identifier in ids:
                cameras.stop(identifier)
            cls.server.shutdown()
            cls.server.server_close()
            cls.worker.join(timeout=5)
            cls.admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
            cls.admin.close()
        cls.addClassCleanup(stop_and_drop)

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
        return response.status, json.loads(data) if data else None

    def test_model_gate_hot_cameras(self):
        """Gate model (owner 13 Nov): _hot_cameras() hanya memuat kamera yang
        punya unit dengan operator sedang operating — konsisten OperationStatus
        (assignment aktif + sesi hidup − binding NULL eksplisit). TTL cache
        dipaksa basi untuk menguji tiap transisi deterministik."""
        import time as _time
        from Sources.cameras.playback import CameraManager
        hse = self.token('cam-hse')
        status, camera = self.request('POST', '/cameras',
                                     {'name': 'Gate Cam', 'type': 'rtsp',
                                      'source': 'rtsp://127.0.0.1:1/gate'},
                                     token=hse)
        self.assertEqual(status, 201)
        manager = CameraManager(self.factory)
        manager._hot_cache = (0.0, set())
        self.assertEqual(manager._hot_cameras(), set(), 'tanpa unit → kamera dingin')

        equipment = f'gate-{uuid4().hex[:8]}-eq-1'
        now_ms = int(_time.time() * 1000)
        with self.factory() as c:
            hse_id = c.execute("SELECT id FROM users WHERE username='cam-hse'").fetchone()[0]
            op_id = c.execute(
                "INSERT INTO users(username, display_name, password_hash, role) "
                "VALUES (%s, 'Op Gate', 'x', 'operator') RETURNING id",
                ('gate-op-' + uuid4().hex[:8],)).fetchone()[0]
            c.execute("INSERT INTO equipment_instances(equipment_instance_id, name) "
                      "VALUES (%s, 'Gate Excavator')", (equipment,))
            c.execute("INSERT INTO equipment_cameras(equipment_instance_id, camera_id, "
                      "created_at_ms, created_by) VALUES (%s, %s, %s, %s)",
                      (equipment, camera['id'], now_ms, hse_id))
            c.execute("INSERT INTO sessions(token_hash, user_id, expires_at) "
                      "VALUES (%s, %s, now() + interval '1 day')",
                      (uuid4().hex + uuid4().hex[:32], op_id))
            c.execute("INSERT INTO assignments(equipment_instance_id, operator_id, "
                      "assigned_at_ms, status) VALUES (%s, %s, %s, 'active')",
                      (equipment, op_id, now_ms))
            c.execute("INSERT INTO shift_bindings(operator_id, equipment_instance_id, "
                      "set_at_ms, operation_started_at_ms) "
                      "VALUES (%s, %s, %s, %s)",
                      (op_id, equipment, now_ms, now_ms))   # Start Operating
        manager._hot_cache = (0.0, set())
        self.assertEqual(manager._hot_cameras(), {camera['id']},
                         'unit + operator operating → kamera hot')

        # Stop eksplisit (binding NULL) → keluar dari set hot.
        with self.factory() as c:
            c.execute("UPDATE shift_bindings SET operation_started_at_ms=NULL "
                      "WHERE equipment_instance_id=%s", (equipment,))
        manager._hot_cache = (0.0, {camera['id']})
        self.assertEqual(manager._hot_cameras(), set(),
                         'operator berhenti → kamera kembali dingin')

    def token(self, username):
        from Sources.auth.service import AuthService
        return AuthService(self.factory).login(username, 'temporary-test-password')['access_token']

    def test_camera_stream_mjpeg_frames(self):
        """GET /cameras/{id}/stream.mjpeg — live feed multipart (api-draft 4.3 S2):
        header multipart/x-mixed-replace + boundary, frame pertama langsung
        dikirim, frame berikut datang saat counter berubah (JPEG valid)."""
        hse = self.token('cam-hse')
        status, camera = self.request('POST', '/cameras',
                                      {'name': 'Stream Cam', 'type': 'recording',
                                       'source': str(self.video), 'playbackFps': 12, 'loop': True},
                                      token=hse)
        self.assertEqual(status, 201)
        self.assertEqual(self.request('POST', f"/cameras/{camera['id']}/start",
                                      token=hse)[0], 200)
        time.sleep(1.0)  # biarkan frame pertama tersimpan

        client = http.client.HTTPConnection(*self.server.server_address, timeout=20)
        client.request('GET', f"/cameras/{camera['id']}/stream.mjpeg",
                       headers={'Authorization': 'Bearer ' + hse})
        response = client.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn('multipart/x-mixed-replace', response.getheader('Content-Type'))
        self.assertIn('boundary=frame', response.getheader('Content-Type'))

        def read_frame(read):
            seen = b''
            while b'Content-Length:' not in seen:
                seen += read(1)
            header = seen.split(b'Content-Length:')[1]
            while b'\r\n' not in header:
                header += read(1)
            length = int(header.split(b'\r\n')[0])
            read(2)  # CRLF sebelum body
            body = read(length)
            self.assertTrue(body.startswith(b'\xff\xd8'))   # SOI JPEG
            self.assertTrue(body.rstrip().endswith(b'\xff\xd9'))  # EOI JPEG
            return body

        first = read_frame(response.read)
        second = read_frame(response.read)
        self.assertNotEqual(first, second)  # frame berubah (video berjalan; byte awal JPEG memang identik antar frame)
        client.close()

        # Kamera tanpa frame → 503 camera_offline (bukan stream menggantung).
        # Register auto-start (13 Nov): kamera rekaman pasti pernah berframe —
        # jalur tanpa-frame yang deterministik = RTSP produser mati.
        status, other = self.request('POST', '/cameras',
                                     {'name': 'Idle Cam', 'type': 'rtsp',
                                      'source': 'rtsp://127.0.0.1:1/idle'},
                                     token=hse)
        self.assertEqual(status, 201)
        idle = http.client.HTTPConnection(*self.server.server_address, timeout=20)
        idle.request('GET', f"/cameras/{other['id']}/stream.mjpeg",
                     headers={'Authorization': 'Bearer ' + hse})
        idle_response = idle.getresponse()
        idle_response.read()
        self.assertEqual(idle_response.status, 503)

    def test_camera_lifecycle_over_http(self):
        hse = self.token('cam-hse')
        operator = self.token('cam-op')

        # POST /cameras — 201; non-hse 403; validasi badan
        body = {'name': 'Test Cam', 'location': 'Tes Timur', 'type': 'recording',
                'source': str(self.video), 'playbackFps': 12, 'loop': False}
        status, camera = self.request('POST', '/cameras', body, token=hse)
        self.assertEqual(status, 201)
        # Register auto-start (owner 13 Nov): status langsung 'online'.
        self.assertEqual(camera['status'], 'online')
        self.assertEqual(camera['playbackFps'], 12)
        self.assertTrue(camera['loop'] is False)
        status, _ = self.request('POST', '/cameras', body, token=operator)
        self.assertEqual(status, 403)
        status, payload = self.request('POST', '/cameras', {'name': 'x', 'type': 'recording'}, token=hse)
        self.assertEqual(status, 400)
        # duplikat sumber → 409; file hilang → 404; rtsp diterima sbg definisi
        status, payload = self.request('POST', '/cameras', body, token=hse)
        self.assertEqual((status, payload['error']['code']), (409, 'already_exists'))
        status, payload = self.request('POST', '/cameras',
                                       {'name': 'Missing', 'type': 'recording',
                                        'source': '/tidak/ada.mp4'}, token=hse)
        self.assertEqual((status, payload['error']['code']), (404, 'file_not_found'))
        status, rtsp = self.request('POST', '/cameras',
                                    {'name': 'Rtsp Nanti', 'type': 'rtsp',
                                     'source': 'rtsp://127.0.0.1:1/x'}, token=hse)
        self.assertEqual(status, 201)
        # rtsp tapi URL-nya bukan rtsp:// → 400 (api-draft 4.3)
        status, payload = self.request('POST', '/cameras',
                                       {'name': 'Rtsp Salah', 'type': 'rtsp',
                                        'source': 'http://bukan-rtsp'}, token=hse)
        self.assertEqual(status, 400)

        # GET list/detail
        status, listing = self.request('GET', '/cameras', token=hse)
        self.assertEqual(status, 200)
        self.assertEqual({c['name'] for c in listing['items']}, {'Test Cam', 'Rtsp Nanti'})
        status, detail = self.request('GET', f"/cameras/{camera['id']}", token=hse)
        self.assertEqual(detail['location'], 'Tes Timur')
        status, _ = self.request('GET', '/cameras', token=operator)
        self.assertEqual(status, 403)

        # Register auto-start (owner 13 Nov 2026): kamera rekaman langsung
        # streaming setelah register. Snapshot 503 kini hanya pada kamera yang
        # TIDAK PERNAH menghasilkan frame — diuji lewat RTSP produser mati.
        status, _, _ = self.request('GET', f"/cameras/{rtsp['id']}/snapshot.jpg",
                                     token=hse, raw=True)
        self.assertEqual(status, 503)

        # start → menunggu frame mengalir → snapshot JPEG + ETag (annotated & raw)
        status, started = self.request('POST', f"/cameras/{camera['id']}/start", token=hse)
        self.assertEqual(status, 200)
        self.assertEqual(started['status'], 'online')
        # start kamera rtsp live → 200 online; port tertutup = open gagal instan
        # (tanpa keluar jaringan) — 3x gagal beruntun → fallback (lihat bawah).
        status, started_rtsp = self.request('POST', f"/cameras/{rtsp['id']}/start", token=hse)
        self.assertEqual((status, started_rtsp['status']), (200, 'online'))
        deadline = time.monotonic() + 15
        snap_status, snap_bytes, snap_headers = 503, b'', {}
        while time.monotonic() < deadline and snap_status != 200:
            snap_status, snap_bytes, snap_headers = self.request(
                'GET', f"/cameras/{camera['id']}/snapshot.jpg", token=hse, raw=True)
            if snap_status != 200:
                time.sleep(0.4)
        self.assertEqual(snap_status, 200, 'snapshot harus tersedia setelah start')
        self.assertTrue(snap_bytes.startswith(b'\xff\xd8'), 'JPEG magic')
        self.assertIn('ETag', snap_headers)
        first_etag = snap_headers['ETag']
        status, raw_bytes, raw_headers = self.request(
            'GET', f"/cameras/{camera['id']}/snapshot.jpg?annotated=false",
            token=hse, raw=True)
        self.assertEqual(status, 200)
        self.assertNotEqual(raw_headers['ETag'].split('-')[1], first_etag.split('-')[1] or 'x',
                            'annotated vs raw harus berbeda suffix')
        self.assertIn('max-age=5', raw_headers['Cache-Control'])

        # stop → offline; snapshot tetap ada (frame terakhir) tapi status offline
        status, stopped = self.request('POST', f"/cameras/{camera['id']}/stop", token=hse)
        self.assertEqual((status, stopped['status']), (200, 'offline'))
        self.assertEqual(self.server.context.cameras.is_running(camera['id']), False)

        # DELETE — stop otomatis + hapus; 404 bila dua kali
        status, payload = self.request('DELETE', f"/cameras/{camera['id']}", token=hse)
        self.assertEqual((status, payload['ok']), (200, True))
        status, payload = self.request('DELETE', f"/cameras/{camera['id']}", token=hse)
        self.assertEqual(status, 404)
        # Fallback RTSP: produser tidak pernah ada → 3x open gagal beruntun
        # (reconnect_delay 2 s → ±6–8 s) → scan berhenti, status error +
        # healthNote fallback — bukan reconnect selamanya.
        deadline = time.monotonic() + 30
        rtsp_detail = None
        while time.monotonic() < deadline:
            status, rtsp_detail = self.request('GET', f"/cameras/{rtsp['id']}",
                                                token=hse)
            note = rtsp_detail['healthNote'] or ''
            if rtsp_detail['status'] == 'error' and 'fallback' in note:
                break
            time.sleep(0.5)
        self.assertEqual((status, rtsp_detail['status']), (200, 'error'),
                         'RTSP produser mati wajib fallback ke error setelah 3 percobaan')
        self.assertIn('fallback', rtsp_detail['healthNote'] or '')
        # stop setelah fallback → idempotent: playback sudah berhenti sendiri,
        # handler menjawab status terkini (error), bukan offline.
        status, stopped_rtsp = self.request('POST', f"/cameras/{rtsp['id']}/stop", token=hse)
        self.assertEqual((status, stopped_rtsp['status']), (200, 'error'))
        status, payload = self.request('DELETE', f"/cameras/{rtsp['id']}", token=hse)
        self.assertEqual((status, payload['ok']), (200, True))
        status, listing = self.request('GET', '/cameras', token=hse)
        self.assertEqual(listing['items'], [])


class IdleKeepaliveTests(unittest.TestCase):
    """Stride keepalive kamera dingin — murni unit, tanpa DB/server."""
    def test_stride_matches_keepalive_rate(self):
        from Sources.cameras.playback import _idle_keepalive_stride, IDLE_KEEPALIVE_FPS
        self.assertGreater(IDLE_KEEPALIVE_FPS, 0, 'default harus > 0 (feed tetap hidup)')
        # 8 fps sumber pada 1 fps keepalive → simpan tiap frame ke-8.
        self.assertEqual(_idle_keepalive_stride(8.0),
                         max(1, round(8.0 / IDLE_KEEPALIVE_FPS)))
        # Sumber lebih lambat dari keepalive → tiap frame tetap disimpan.
        self.assertEqual(_idle_keepalive_stride(0.5), 1)

    def test_zero_disables_store(self):
        import Sources.cameras.playback as playback
        original = playback.IDLE_KEEPALIVE_FPS
        playback.IDLE_KEEPALIVE_FPS = 0
        try:
            self.assertEqual(playback._idle_keepalive_stride(8.0), 0)
        finally:
            playback.IDLE_KEEPALIVE_FPS = original


class RetryBackoffTests(unittest.TestCase):
    """Backoff retry RTSP — murni unit, tanpa DB/server."""

    def test_backoff_doubles_then_caps(self):
        import Sources.cameras.playback as playback
        manager = playback.CameraManager()
        waits = [manager._note_retry('cam-1', 0)[0]
                 for _ in range(8)]
        self.assertEqual(waits[:5], [30.0, 60.0, 120.0, 240.0, 480.0])
        self.assertEqual(waits[5:], [600.0, 600.0, 600.0],
                         'cap RTSP_RETRY_MAX_S menahan spam tiap 30 dtk')

    def test_counters_are_per_camera(self):
        import Sources.cameras.playback as playback
        manager = playback.CameraManager()
        manager._note_retry('a', 0)
        manager._note_retry('a', 0)
        wait_b, attempt_b = manager._note_retry('b', 0)
        self.assertEqual((wait_b, attempt_b), (30.0, 1),
                         'kamera sehat tak ikut backoff kamera mati')


if __name__ == '__main__':
    unittest.main()
