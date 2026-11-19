"""Seam identitas unit — keputusan owner 14 Nov 2026 (bug relink RTSP).

Kamera yang playing menghasilkan identitas fallback "{prefix-kamera}-eq-1",
padahal unit ter-DAFTAR (assignment/operating/radius, app fleet) bisa ber-ID
lain bila kameranya di-relink script produser (kasus lapangan: kamera RTSP
`245eed6d…` menyuapi evaluator `245eed6d-eq-1`, sementara unit operating
terdaftar `44630dad-eq-1`). Seam: resolver kamera→unit dipasang composition
root; trackId nyata tetap menang atas apa pun; tanpa resolver, perilaku
fallback lama terjaga.
"""
import os
import sys
import time
import unittest
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Sources.core.evaluator import RiskEvaluator


def detection(label, box, **extra):
    return {'label': label, 'classId': 0 if label == 'excavator' else 2,
            'confidence': 0.9, 'box': box, **extra}


EXCAVATOR = detection('excavator', (100, 100, 500, 300))
NEAR_PERSON = detection('person', (400, 250, 420, 290))  # dalam inflate padding


def _fast_evaluator(**params):
    """Evaluator piksel + histeresis nol (konfirmasi instant di test)."""
    return RiskEvaluator(confirm_time_s=0, clear_time_s=0, window_s=1, **params)


class IdentitySeamCoreTests(unittest.TestCase):
    """core/evaluator: identity_for hanya mengganti identitas ANONIM."""

    def _enter_zone(self, evaluator, source='cam-245eed6d-xxxx', n=3):
        """Feed sampai transisi terbit; return alert pertama (atau None)."""
        for step in range(n):
            alert = evaluator.evaluate([EXCAVATOR, NEAR_PERSON],
                                       timestamp_ms=1000 + step, source_id=source,
                                       now_ms=10_000 + step * 300)
            if alert is not None:
                return alert
        return None

    def test_identity_for_mengganti_identitas_anonim(self):
        evaluator = _fast_evaluator(
            identity_for=lambda source_id: '44630dad-eq-1',
            operating_for=lambda _identity: True)
        alert = self._enter_zone(evaluator)
        self.assertIsNotNone(alert, 'person di zona wajib menghasilkan alert')
        self.assertEqual(alert['equipmentInstanceId'], '44630dad-eq-1',
                         'payload alert wajib id unit ter-DAFTAR (relink), bukan prefix kamera')
        status = evaluator.unit_statuses('cam-245eed6d-xxxx')[0]
        self.assertEqual(status['equipmentInstanceId'], '44630dad-eq-1')
        self.assertEqual(status['operationActive'], True)

    def test_identity_tidak_menimpa_track_id(self):
        """trackId nyata tetap identitas final — resolver hanya fallback anonim."""
        evaluator = _fast_evaluator(
            identity_for=lambda source_id: '44630dad-eq-1',
            operating_for=lambda _identity: True)
        excavator = detection('excavator', (100, 100, 500, 300), trackId='track-9')
        alert = None
        for step in range(3):
            alert = evaluator.evaluate([excavator, NEAR_PERSON],
                                       timestamp_ms=1000 + step, source_id='cam-x',
                                       now_ms=10_000 + step * 300)
            if alert is not None:
                break
        self.assertIsNotNone(alert)
        self.assertEqual(alert['equipmentInstanceId'], 'track-9')

    def test_tanpa_identity_for_fallback_prefix_tetap(self):
        evaluator = _fast_evaluator(operating_for=lambda _identity: True)
        alert = self._enter_zone(evaluator, source='245eed6d-9999-zzzz')
        self.assertEqual(alert['equipmentInstanceId'], '245eed6d-eq-1',
                         'regresi: perilaku fallback lama wajib utuh')

    def test_operating_gate_terbuka_saat_identitas_cocok(self):
        """Uang: relink menyebabkan operating_for(fallback) False — resolver
        menjembatani sehingga alert tetap terbit."""
        def operating_for(identity):
            return identity == '44630dad-eq-1'  # hanya unit ter-DAFTAR operating

        evaluator = _fast_evaluator(identity_for=lambda source_id: '44630dad-eq-1',
                                     operating_for=operating_for)
        alert = self._enter_zone(evaluator)
        self.assertIsNotNone(alert, 'seam harus menjembatani identitas → gate terbuka')
        self.assertEqual(alert['kind'], 'person_entered_zone')


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1',
                     'Requires local PostgreSQL')
class CameraIdentityCatalogTests(unittest.TestCase):
    """Registry: peta kamera→unit ter-DAFTAR, sadar-relink, TTL reload."""

    @classmethod
    def setUpClass(cls):
        from Sources.infrastructure.database.connection import connect, database_url
        from Sources.infrastructure.database.migrate import migrate
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        import psycopg as psycopg_mod

        cls.admin = psycopg_mod.connect('dbname=postgres host=/tmp', autocommit=True)
        name = 'safety_ident_test_' + uuid4().hex
        cls.admin.execute(sql.SQL('CREATE DATABASE {} OWNER safety_app').format(sql.Identifier(name)))
        conninfo = make_conninfo(database_url(), dbname=name)
        cls.conninfo = conninfo
        with connect(conninfo) as connection:
            migrate(connection)
        from Sources.users.create import create_user
        cls.hse_id = create_user('ident-hse', 'HSE Ident', 'hse', 'password-demo-2026',
                                 lambda: connect(conninfo))

        def stop_and_drop():
            cls.admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
            cls.admin.close()
        cls.addClassCleanup(stop_and_drop)

    def _camera(self, name):
        from Sources.cameras.service import create_camera
        from Sources.infrastructure.database.connection import connect
        return create_camera(name, 'Tes', 'rtsp', f'rtsp://127.0.0.1:8554/{uuid4().hex}',
                             None, False, int(time.time() * 1000),
                             lambda: connect(self.conninfo))

    def test_peta_kamera_unit_dan_relink(self):
        from Sources.equipment import registry
        from Sources.equipment.registry import CameraIdentityCatalog
        from Sources.infrastructure.database.connection import connect

        camera_old = self._camera('Cam Lama')
        unit = registry.create_from_camera(camera_old['id'], 'Unit Relink', self.hse_id,
                                           int(time.time() * 1000),
                                           connection_factory=lambda: connect(self.conninfo))
        canonical = unit['equipmentInstanceId']

        catalog = CameraIdentityCatalog(lambda: connect(self.conninfo), ttl_s=60)
        self.assertEqual(catalog.equipment_for(camera_old['id']), canonical)

        # Relink ala script produser: unit lama dipindah ke kamera RTSP baru.
        camera_new = self._camera('Cam Baru')
        with connect(self.conninfo) as connection:
            connection.execute('UPDATE equipment_cameras SET camera_id=%s '
                               'WHERE equipment_instance_id=%s',
                               (camera_new['id'], canonical))

        # Katalog baru (TTL kritis di uji integrasi, bukan unit) melihat relink.
        fresh = CameraIdentityCatalog(lambda: connect(self.conninfo), ttl_s=0)
        self.assertEqual(fresh.equipment_for(camera_new['id']), canonical,
                         'kamera hasil relink wajib memetakan ke unit ter-daftar')
        self.assertIsNone(fresh.equipment_for(camera_old['id']),
                          'kamera lama tidak lagi ter-link unit')

    def test_evaluator_pakai_katalog_relink_alert_terbit(self):
        """Uang level integrasi evaluator+registry: feed dari KAMERA baru
        (identitas fallback prefix kamera baru) → alert tetap bertanda unit
        ter-DAFTAR dan operating gate terbuka."""
        from Sources.equipment import registry
        from Sources.equipment.registry import CameraIdentityCatalog
        from Sources.assignments.service import create_assignment
        from Sources.infrastructure.database.connection import connect

        camera_old = self._camera('Cam Asal')
        unit = registry.create_from_camera(camera_old['id'], 'Unit Uang', self.hse_id,
                                           int(time.time() * 1000),
                                           connection_factory=lambda: connect(self.conninfo))
        canonical = unit['equipmentInstanceId']
        camera_new = self._camera('Cam Pengganti')
        with connect(self.conninfo) as connection:
            connection.execute('UPDATE equipment_cameras SET camera_id=%s '
                               'WHERE equipment_instance_id=%s',
                               (camera_new['id'], canonical))

        catalog = CameraIdentityCatalog(lambda: connect(self.conninfo), ttl_s=0)
        evaluator = _fast_evaluator(
            identity_for=catalog.equipment_for,
            operating_for=lambda identity: identity == canonical)

        alert = None
        for step in range(3):
            alert = evaluator.evaluate([EXCAVATOR, NEAR_PERSON],
                                       timestamp_ms=1000 + step,
                                       source_id=camera_new['id'],
                                       now_ms=10_000 + step * 300)
            if alert is not None:
                break
        self.assertIsNotNone(alert)
        self.assertEqual(alert['kind'], 'person_entered_zone')
        self.assertEqual(alert['equipmentInstanceId'], canonical)


if __name__ == '__main__':
    unittest.main()
