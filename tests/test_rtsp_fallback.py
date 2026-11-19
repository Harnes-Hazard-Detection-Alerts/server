"""Fallback kamera RTSP — 3 open gagal beruntun = scan berhenti (12 Nov 2026).

Tanpa PostgreSQL dan tanpa cv2: RTSPReader di-injeksi modul VideoCapture
palsu; CameraManager diuji dengan reader stub. Perilaku yang dijaga:
budget max_failed_opens tercapai → generator berakhir (tidak re-open
selamanya), status kamera error + healthNote fallback, thread keluar
bersih, frame terakhir tetap tersedia untuk snapshot.
"""
import sys
import time
import unittest
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _FakeCV2:
    """Namespace pengganti cv2 — hanya konstanta yang dipakai RTSPReader."""

    CAP_PROP_FPS = 'fps'
    CAP_PROP_FRAME_WIDTH = 'w'
    CAP_PROP_FRAME_HEIGHT = 'h'


class _FakeCapture:
    """Pengganti cv2.VideoCapture: isOpened/read/get/release terkontrol."""

    def __init__(self, openable, frames=()):
        self.openable = openable
        self._frames = list(frames)
        self.released = False

    def isOpened(self):
        return self.openable

    def read(self):
        return (True, self._frames.pop(0)) if self._frames else (False, None)

    def get(self, prop):
        return {'fps': 30.0, 'w': 640, 'h': 480}.get(prop, 0)

    def release(self):
        self.released = True


class RTSPFallbackTests(unittest.TestCase):
    """Budget reconnect RTSPReader — berhenti setelah 3 open gagal."""

    def _reader(self, captures, on_state=None):
        from Sources.video_input.reader import RTSPReader
        opens = []

        def factory(url):
            capture = captures.pop(0)
            opens.append(capture)
            return capture

        reader = RTSPReader('rtsp://127.0.0.1:8554/mac', reconnect_delay=0,
                            on_state=on_state, max_failed_opens=3,
                            cv2_module=_FakeCV2, capture_factory=factory)
        return reader, opens

    def test_tiga_open_gagal_menghentikan_scan(self):
        """3× DESCRIBE gagal (404) → frames() berakhir, tidak scan abadi."""
        states = []
        reader, opens = self._reader(
            [_FakeCapture(False) for _ in range(99)], on_state=states.append)
        frames = list(reader.frames())
        self.assertEqual(frames, [])
        self.assertEqual(len(opens), 3, 'scan wajib berhenti di percobaan ke-3')
        self.assertEqual(states, ['stale'], 'fallback tercatat saat budget habis')

    def test_open_sukses_mereset_budget(self):
        """Stream hidup lalu mati → reconnect dihitung dari nol; 3 gagal → stop."""
        states = []
        reader, opens = self._reader(
            [_FakeCapture(True, frames=['frame-0', 'frame-1']),
             _FakeCapture(False), _FakeCapture(False), _FakeCapture(False),
             _FakeCapture(False)],  # capture kelima tak boleh terpakai — scan sudah berhenti di open ke-4
            on_state=states.append)
        frames = list(reader.frames())
        self.assertEqual([frame for _, _, frame in frames],
                         ['frame-0', 'frame-1'], 'frame live terlebih dulu')
        self.assertEqual(len(opens), 4, '1 sukses + 3 gagal, lalu berhenti')
        self.assertEqual(states, ['stale'])

    def test_release_menghentikan_scan_sebelum_budget(self):
        """stop() (HSE) tetap berlaku kapan pun — release di tengah reconnect."""
        reader, _opens = self._reader([_FakeCapture(False) for _ in range(99)])
        generator = reader.frames()
        reader.release()  # sebelum frames() konsumsi iterasi pertama
        self.assertEqual(list(generator), [])


class CameraManagerFallbackTests(unittest.TestCase):
    """Manager: reader RTSP berakhir (budget habis) → tidak re-open, status error."""

    def test_rtsp_giveup_tidak_reopen_dan_status_fallback(self):
        from Sources.cameras.playback import CameraManager

        class _DeadReader:
            """Pengganti RTSPReader budget habis: frames() langsung berakhir."""

            effective_fps = 0.0

            def frames(self):
                return iter(())

            def release(self):
                pass

        class _Manager(CameraManager):
            def __init__(self):
                super().__init__()
                self.transitions = []
                self.opens = 0

            def _open_reader(self, camera):
                self.opens += 1
                if self.opens > 1:
                    raise RuntimeError('RE-OPEN SETELAH FALLBACK — tidak boleh')
                return _DeadReader()

            def _set_status(self, camera_id, status, health_note):
                self.transitions.append((status, health_note))

        manager = _Manager()
        camera_id = uuid4().hex
        manager.start({'id': camera_id, 'name': 'Site Live', 'type': 'rtsp',
                       'source': 'rtsp://127.0.0.1:8554/mac'})
        deadline = time.monotonic() + 5
        while manager.is_running(camera_id) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(manager.is_running(camera_id), 'thread harus keluar')
        self.assertEqual(manager.opens, 1, 'reader tidak boleh dibuka ulang')
        self.assertEqual(len(manager.transitions), 2,
                         'transisi: online (start) → error (fallback)')
        final = manager.transitions[-1]
        self.assertEqual(final[0], 'error')
        self.assertIn('fallback', str(final[1]),
                      'healthNote harus menyebut fallback untuk dashboard HSE')


if __name__ == '__main__':
    unittest.main()
