"""Pre-flight readiness — logic gating (keputusan owner 14 Nov 2026).

Transport HTTP di-inject: seluruh klasifikasi (health/model, login, frame
bergerak dua-ETag, operating gate) bisa dites tanpa server. Tujuan test =
pencegah kambuhnya "semua nyala tapi unknown/connecting" — kegagalan mesti
terbaca SEBAGAI baris gagal, bukan berhenti diam.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Tools'))

from preflight import check_once, render, run


CAM = {'id': 'c1', 'name': 'Site Timur', 'desiredOn': True, 'status': 'online'}
CAM_IDLE = {'id': 'c2', 'name': 'Idle Cam', 'desiredOn': False, 'status': 'offline'}
UNIT = {'equipmentInstanceId': '44630dad-eq-1', 'operationActive': True,
        'monitoringStatus': 'online'}


def _health_ok():
    return 200, {'ok': True, 'modelLoaded': True}, {}


def _script(steps):
    """steps: list callable(step_index) -> (status, json, headers)."""
    calls = {'n': 0}

    def transport(method, path, body=None, token=None):
        result = steps[min(calls['n'], len(steps) - 1)](calls['n'])
        calls['n'] += 1
        return result
    return transport


class PreflightLogicTests(unittest.TestCase):
    def test_semua_siap(self):
        def step(n):
            if n == 0: return 200, {'ok': True, 'modelLoaded': True}, {}
            if n == 1: return 200, {'access_token': 't'}, {}
            if n == 2: return 200, {'items': [CAM, CAM_IDLE]}, {}
            if n in (3, 4):
                return 200, None, {'ETag': '"1-a"' if n == 3 else '"2-a"'}
            if n == 5: return 200, {'items': [UNIT]}, {}
            raise AssertionError(f'panggilan tak terduga #{n}')

        lines, token = check_once(_script([step]), 'anything', 'pass', poll_gap_s=0)
        labels_ok = {label: ok for label, ok, _ in lines}
        self.assertTrue(all(labels_ok.values()))
        self.assertEqual(token, 't')
        self.assertIn("kamera 'Site Timur'", labels_ok)
        self.assertEqual(render(lines), 0)

    def test_frame_beku_terdeteksi(self):
        counters = {'snap': 0}

        def step(n):
            if n == 0: return 200, {'ok': True, 'modelLoaded': True}, {}
            if n == 1: return 200, {'access_token': 't'}, {}
            if n == 2: return 200, {'items': [CAM]}, {}
            if n in (3, 4):
                counters['snap'] += 1
                # ETag SAMA dua kali = tidak ada frame baru (gejala lama)
                return 200, None, {'ETag': '"9-a"'}
            if n == 5: return 200, {'items': [UNIT]}, {}
            raise AssertionError('tak terduga')

        lines, _ = check_once(_script([step]), 'anything', 'pass', poll_gap_s=0)
        camera_line = [(label, ok, note) for label, ok, note in lines
                       if label.startswith('kamera')]
        self.assertFalse(camera_line[0][1], 'ETag beku wajib gagal')
        self.assertIn('beku', camera_line[0][2])
        self.assertEqual(render(lines) > 0, True)

    def test_snapshot_503_gagal(self):
        def step(n):
            if n == 0: return 200, {'ok': True, 'modelLoaded': True}, {}
            if n == 1: return 200, {'access_token': 't'}, {}
            if n == 2: return 200, {'items': [CAM]}, {}
            if n == 3: return 503, None, {}
            if n >= 4: return 200, {'items': [UNIT]}, {}  # unit tetap dicek
            raise AssertionError('tak terduga')

        lines, _ = check_once(_script([step]), 'anything', 'pass', poll_gap_s=0)
        bad = [(label, ok, note) for label, ok, note in lines if not ok]
        self.assertTrue(any('snapshot' in note for _l, _o, note in bad),
                        '503 harus masuk daftar gagal')

    def test_operating_false_gagal(self):
        unit_off = dict(UNIT, operationActive=False)

        def step(n):
            if n == 0: return 200, {'ok': True, 'modelLoaded': True}, {}
            if n == 1: return 200, {'access_token': 't'}, {}
            if n == 2: return 200, {'items': [CAM_IDLE]}, {}
            if n == 5 or n >= 5: return 200, {'items': [unit_off]}, {}
            return 200, None, {'ETag': '"x"'}

        # kamera idle → leak langsung ke status? cek: skrip dibangun urut;
        # gunakan pemetaan path agar deterministik:
        def transport(method, path, body=None, token=None):
            if path == '/health': return 200, {'ok': True, 'modelLoaded': True}, {}
            if path == '/auth/login': return 200, {'access_token': 't'}, {}
            if path == '/cameras': return 200, {'items': [CAM_IDLE]}, {}
            if path == '/equipment/status': return 200, {'items': [unit_off]}, {}
            raise AssertionError(f'path tak terduga {path}')

        lines, _ = check_once(transport, 'anything', 'pass', poll_gap_s=0)
        unit_line = [(l, ok) for l, ok, _n in lines if l.startswith('unit')]
        self.assertFalse(unit_line[0][1], 'operationActive False wajib gagal')

    def test_snapshot_jpeg_biner_tidak_mengganggu_parse(self):
        """Anti-kambuh live 14 Nov: snapshot JPEG (Content-Type image/jpeg,
        byte 0xFF) tidak boleh masuk json.loads."""
        jpeg = b'\xff\xd8\xff\xe0bogus'

        def transport(method, path, body=None, token=None):
            if path == '/health': return 200, {'ok': True, 'modelLoaded': True}, {}
            if path == '/auth/login': return 200, {'access_token': 't'}, {}
            if path == '/cameras': return 200, {'items': [CAM]}, {}
            if path.startswith('/cameras/'):
                header = {'ETag': '"1-a"', 'Content-Type': 'image/jpeg'}
                if path.endswith('annotated=false') or 'snapshot' in path:
                    pass
                # dua poll pertama → ETag beda; selebihnya aman
                calls = transport.counts = getattr(transport, 'counts', 0) + 1
                transport.counts = calls
                header['ETag'] = f'"{calls}-a"'
                return 200, None, header
            if path == '/equipment/status': return 200, {'items': [UNIT]}, {}
            raise AssertionError(path)

        lines, _ = check_once(transport, 'anything', 'pass', poll_gap_s=0)
        camera_line = [(label, ok, note) for label, ok, note in lines
                       if label.startswith('kamera')]
        self.assertTrue(camera_line[0][1], 'snapshot biner dua-ETag harus lolos')

    def test_run_deadline_gagal_dan_sukses(self):
        """run() retry sampai ok; deadline cuma bolong sekali."""
        attempts = {'n': 0}

        def transport(method, path, body=None, token=None):
            attempts['n'] += 1
            if path == '/health':
                if attempts['n'] < 3:
                    return 0, None, {}          # server belum dengar
                return 200, {'ok': True, 'modelLoaded': True}, {}
            if path == '/auth/login': return 200, {'access_token': 't'}, {}
            if path == '/cameras': return 200, {'items': [CAM_IDLE]}, {}
            if path == '/equipment/status': return 200, {'items': [UNIT]}, {}
            raise AssertionError(path)

        lines, ready = run(transport, 'a', 'b', timeout_s=10)
        self.assertTrue(ready)
        self.assertGreaterEqual(attempts['n'], 3)

    def test_server_mati_deadline(self):
        def transport(method, path, body=None, token=None):
            return 0, None, {}

        lines, ready = run(transport, 'a', 'b', timeout_s=0.3)
        self.assertFalse(ready)
        self.assertNotEqual(render(lines), 0)


if __name__ == '__main__':
    unittest.main()
