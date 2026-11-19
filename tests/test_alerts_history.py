"""Integration test riwayat alert (DB-gated) — pipeline nyata sampai HTTP.

Alert diproduksi RiskEvaluator asli lewat AlertServer.process_frame (stub
detector), disimpan AlertStore, lalu dibaca via GET /alerts, /alerts/{id},
dan /me/alerts (operator hanya equipment dalam shift binding aktif).
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


def detection(cid, box, track=None):
    item = {'label': 'x', 'classId': cid, 'confidence': 0.9, 'box': box}
    if track:
        item['trackId'] = track
    return item


EXCAVATOR = detection(0, (100, 100, 500, 300), 'cam01-eq-1')
# Pekerja di SEBELAH mesin (luar bbox, masih dalam zona piksel victim) —
# bukan DI DALAM bbox: person terkurung bbox excavator = operator
# (kebijakan 11-12 Nov) dan tak dinilai zona.
NEAR = detection(2, (505, 250, 525, 290))
FAR = detection(2, (700, 500, 720, 540))

BASE = 1794000100000  # epoch-ms bebas-kuari; hanya angka berurut yang dipakai


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1',
                     'Requires local PostgreSQL')
class AlertHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from Sources.alerts.store import AlertStore
        from Sources.alerts.server import AlertServer
        from Sources.alerts.bus import EventBus
        from Sources.api.context import AppContext
        from Sources.auth.service import AuthService
        from Sources.auth.limits import LoginLimits
        from Sources.core.evaluator import RiskEvaluator
        from Sources.infrastructure.database.connection import connect, database_url
        from Sources.infrastructure.database.migrate import migrate
        from Sources.users.create import create_user

        cls.admin = psycopg.connect('dbname=postgres host=/tmp', autocommit=True)
        name = 'safety_alerts_test_' + uuid4().hex
        cls.admin.execute(sql.SQL('CREATE DATABASE {} OWNER safety_app').format(sql.Identifier(name)))

        def factory():
            return connect(make_conninfo(database_url(), dbname=name))
        cls.factory = staticmethod(factory)
        with factory() as connection:
            migrate(connection)
        password = 'temporary-test-password'
        cls.hse = create_user('alerts-hse', 'HSE a', 'hse', password, factory)
        cls.op1 = create_user('alerts-op1', 'Op Satu', 'operator', password, factory)
        cls.server = AlertServer(('127.0.0.1', 0), context=AppContext(
            auth=AuthService(factory), login_limits=LoginLimits(),
            risk=RiskEvaluator(),
            detector=SimpleNamespace(ready=True, detect=lambda frame: frame),
            bus=EventBus(), connect=factory, store=AlertStore(factory)))
        cls.worker = Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

        def stop():
            cls.server.shutdown()
            cls.server.server_close()
            cls.worker.join(timeout=5)

        def drop_database():
            cls.admin.execute(
                sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
            cls.admin.close()
        cls.addClassCleanup(drop_database)  # diregistrasi lebih dulu -> dieksekusi terakhir
        cls.addClassCleanup(stop)           # LIFO: server berhenti sebelum DB dijatuhkan

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

    def token(self, username):
        from Sources.auth.service import AuthService
        return AuthService(self.factory).login(username, 'temporary-test-password')['access_token']

    def test_pipeline_records_and_history_endpoints(self):
        hse = self.token('alerts-hse')
        op1 = self.token('alerts-op1')
        # Binding dibuat setelah login; login baru membersihkan binding dari
        # sesi lama yang sudah kedaluwarsa.
        with self.factory() as connection:
            # FK tim (014): binding mensyaratkan katalog equipment_instance —
            # daftarkan 'cam01-eq-1' dulu (mengikuti pola kamera fallback).
            connection.execute(
                "INSERT INTO equipment_instances(equipment_instance_id, name) "
                "VALUES ('cam01-eq-1', 'Excavator - History') ON CONFLICT DO NOTHING")
            # Routing delivery (resolve_binding_operator) memeriksa assignment
            # aktif + sesi hidup + operation_started — sama dengan gerbang.
            connection.execute(
                'INSERT INTO assignments(equipment_instance_id, operator_id, '
                'assigned_at_ms) VALUES (%s, %s, %s)',
                ('cam01-eq-1', self.op1, BASE))
            connection.execute(
                'INSERT INTO shift_bindings(operator_id, equipment_instance_id, '
                'set_at_ms, operation_started_at_ms) '
                'VALUES (%s, %s, %s, %s)', (self.op1, 'cam01-eq-1', BASE, BASE))
            # Skema 015: alerts.camera_id uuid kamera nyata — kamera uji.
            self.camera_source = connection.execute(
                "INSERT INTO cameras(name, location, type, source, playback_fps, "
                "loop, registered_at_ms) VALUES ('Pipeline Cam', NULL, 'recording', "
                "'/tmp/p.mp4', 8, true, %s) RETURNING id::text",
                (BASE,)).fetchone()[0]

        # Produksi alert lewat pipeline nyata — timing histeresis dijaga:
        # in@0ms, in@800 (CONFIRMED, ditahan jeda), in@2400 (entered terbit),
        # out@3200, out@4000 (time_out cukup -> cleared).
        sequence = [
            (0, [EXCAVATOR, NEAR]),
            (800, [EXCAVATOR, NEAR]),
            (2400, [EXCAVATOR, NEAR]),
            (3200, [EXCAVATOR, FAR]),
            (4000, [EXCAVATOR, FAR]),
            (4800, [EXCAVATOR, FAR]),
        ]
        produced = []
        for offset, dets in sequence:
            _, alert = self.server.process_frame(dets, timestamp_ms=BASE + offset,
                                                 source_id=self.camera_source,
                                                 now_ms=BASE + offset)
            if alert:
                produced.append(alert)
        self.assertEqual([a['kind'] for a in produced],
                         ['person_entered_zone', 'zone_cleared'])
        # Emit async 12 Nov: persist lewat worker — tunggu antrean kosong
        # lalu beri jeda kecil untuk commit item terakhir sebelum menyodori
        # endpoint riwayat (budak daftar langsung kosong).
        for _ in range(200):
            if getattr(self.server, '_emit_queue', None) is None or self.server._emit_queue.empty():
                break
            time.sleep(0.01)
        time.sleep(0.15)

        # GET /alerts (hse)
        status, payload = self.request('GET', '/alerts', token=hse)
        self.assertEqual(status, 200)
        self.assertEqual([item['kind'] for item in payload['items']],
                         ['zone_cleared', 'person_entered_zone'])
        cleared = payload['items'][0]
        entered = payload['items'][1]
        self.assertEqual(entered['equipmentInstanceId'], 'cam01-eq-1')
        self.assertEqual(entered['status'], 'resolved')
        self.assertEqual(entered['resolvedAtMs'], cleared['occurredAtMs'])
        self.assertEqual(len(entered['deliveries']), 1)
        self.assertEqual(
            (entered['deliveries'][0]['channel'], entered['deliveries'][0]['status']),
            ('live', 'failed'))
        status, _ = self.request('GET', '/alerts', token=op1)
        self.assertEqual(status, 403)

        # Filter + cursor
        status, payload = self.request(
            'GET', '/alerts?kind=person_entered_zone&status=resolved', token=hse)
        self.assertEqual([item['kind'] for item in payload['items']], ['person_entered_zone'])
        status, payload = self.request('GET', '/alerts?limit=1', token=hse)
        self.assertEqual(len(payload['items']), 1)
        self.assertIn('nextCursor', payload)
        status, page2 = self.request(
            'GET', f'/alerts?limit=1&cursor={payload["nextCursor"]}', token=hse)
        self.assertEqual(page2['items'][0]['kind'], 'person_entered_zone')
        status, _ = self.request('GET', '/alerts?cursor=bogus', token=hse)
        self.assertEqual(status, 400)

        # Detail + 404
        status, detail = self.request('GET', f"/alerts/{entered['id']}", token=hse)
        self.assertEqual(detail['id'], entered['id'])
        status, _ = self.request('GET', f"/alerts/{uuid4()}", token=hse)
        self.assertEqual(status, 404)

        # /me/alerts: operator hanya equipment aktifnya
        status, payload = self.request('GET', '/me/alerts', token=op1)
        self.assertEqual(status, 200)
        self.assertEqual([item['kind'] for item in payload['items']],
                         ['zone_cleared', 'person_entered_zone'])
        status, payload = self.request('GET', '/me/alerts?status=active', token=op1)
        self.assertEqual(payload['items'], [])
        status, _ = self.request('GET', '/me/alerts', token=hse)
        self.assertEqual(status, 403)

        # Operator tanpa shift binding aktif tidak melihat apa pun
        from Sources.users.create import create_user
        create_user('alerts-op2', 'Op Dua', 'operator',
                    'temporary-test-password', self.factory)
        token2 = self.token('alerts-op2')
        status, payload = self.request('GET', '/me/alerts', token=token2)
        self.assertEqual(payload['items'], [])

    def test_high_risk_replaces_entered_as_active_until_clear(self):
        import uuid as _uuid
        from Sources.alerts.store import AlertStore

        # Skema tim (015/016): alerts.camera_id = uuid kamera nyata dan
        # equipment_instance_id wajib terdaftar di katalog.
        with self.factory() as connection:
            camera_id = connection.execute(
                "INSERT INTO cameras(name, location, type, source, playback_fps, "
                "loop, registered_at_ms) VALUES ('History Cam', NULL, 'recording', "
                "'/tmp/h.mp4', 8, true, %s) RETURNING id::text",
                (BASE,)).fetchone()[0]
            connection.execute(
                "INSERT INTO equipment_instances(equipment_instance_id, name) "
                "VALUES ('high-risk-unit', 'History Excavator') ON CONFLICT DO NOTHING")
            # bebersih bila test sebelumnya bersisa (id identik antar uji)
            connection.execute("DELETE FROM alerts WHERE equipment_instance_id=%s",
                               ('high-risk-unit',))

        store = AlertStore(self.factory)
        common = {
            'message': 'Danger',
            'sourceId': camera_id,
            'equipmentInstanceId': 'high-risk-unit',
            'personsInZone': 1,
        }
        entered = store.record(dict(common, kind='person_entered_zone'), BASE + 10_000)
        high = store.record(dict(common, kind='person_high_risk'), BASE + 11_000)

        active, _ = store.list(equipment_instance_id='high-risk-unit', status='active')
        self.assertEqual([item['kind'] for item in active], ['person_high_risk'])
        self.assertEqual(store.get(entered['id'])['status'], 'resolved')
        self.assertEqual(store.get(high['id'])['status'], 'active')

        store.record(dict(common, kind='zone_cleared', personsInZone=0),
                     BASE + 12_000)
        active, _ = store.list(equipment_instance_id='high-risk-unit', status='active')
        self.assertEqual(active, [])
        with self.factory() as connection:
            connection.execute('DELETE FROM alerts WHERE equipment_instance_id=%s',
                               ('high-risk-unit',))


    def test_alert_summary_aggregates_per_bucket_without_listing_rows(self):
        """GET /alerts/summary — ringkasan per bucket, tanpa menarik semua baris."""
        from Sources.alerts.store import AlertStore

        hse = self.token('alerts-hse')
        with self.factory() as connection:
            camera_id = connection.execute(
                "INSERT INTO cameras(name, location, type, source, playback_fps, "
                "loop, registered_at_ms) VALUES ('Summary Cam', NULL, 'recording', "
                "'/tmp/s.mp4', 8, true, %s) RETURNING id::text",
                (BASE,)).fetchone()[0]
            connection.execute(
                "INSERT INTO equipment_instances(equipment_instance_id, name) "
                "VALUES ('summary-unit', 'Summary Excavator') ON CONFLICT DO NOTHING")
            connection.execute('DELETE FROM alerts WHERE equipment_instance_id=%s',
                               ('summary-unit',))

        store = AlertStore(self.factory)
        common = {'message': 'Danger', 'sourceId': camera_id,
                  'equipmentInstanceId': 'summary-unit', 'personsInZone': 1}
        store.record(dict(common, kind='person_entered_zone'), BASE + 10_000)
        store.record(dict(common, kind='person_entered_zone'), BASE + 20_000)
        store.record(dict(common, kind='zone_cleared', personsInZone=0), BASE + 30_000)
        store.record(dict(common, kind='person_high_risk'), BASE + 3_600_000)

        status, payload = self.request('GET', '/alerts/summary?bucket=hour', token=hse)
        self.assertEqual(status, 200)
        self.assertEqual(payload['bucket'], 'hour')
        self.assertEqual(payload['totals']['person_entered_zone'], 2)
        self.assertEqual(payload['totals']['zone_cleared'], 1)
        self.assertEqual(payload['totals']['person_high_risk'], 1)
        self.assertEqual(payload['activeCount'], 1)      # high_risk masih berdiri
        self.assertGreaterEqual(len(payload['buckets']), 2)
        self.assertEqual(payload['maxBucket']['total'], 3)
        self.assertEqual(payload['top'][0]['equipmentInstanceId'], 'summary-unit')
        self.assertEqual(payload['top'][0]['count'], 4)

        status, daily = self.request('GET', '/alerts/summary?bucket=day', token=hse)
        self.assertEqual(status, 200)
        self.assertEqual(daily['bucket'], 'day')

        status, _ = self.request('GET', '/alerts/summary?bucket=minggu', token=hse)
        self.assertEqual(status, 400)      # bucket tak dikenal ditolak

        with self.factory() as connection:
            connection.execute('DELETE FROM alerts WHERE equipment_instance_id=%s',
                               ('summary-unit',))


if __name__ == '__main__':
    unittest.main()
