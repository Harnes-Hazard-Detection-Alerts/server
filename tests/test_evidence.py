"""Unit test evidence burst recorder + ring-buffer CameraManager.

Tidak membutuhkan PostgreSQL: recorder murni filesystem + cv2; ring-buffer
diuji lewat CameraManager dengan recorder yang disuntik langsung (tanpa
thread playback/DB).
"""
import json
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Sources.cameras.evidence import EvidenceRecorder, get_evidence_files
from Sources.cameras.playback import RING_BUFFER_FRAMES, CameraManager

HAS_CV2 = False
try:
    import cv2
    import numpy
    HAS_CV2 = True
except ImportError:
    pass


def _frame(value=0, size=(64, 64)):
    image = numpy.zeros((size[1], size[0], 3), dtype=numpy.uint8)
    image[:, :, 0] = value % 255
    return image


@unittest.skipUnless(HAS_CV2, 'Requires cv2 + numpy (requirements-ml)')
class EvidenceRecorderTests(unittest.TestCase):
    def test_record_writes_five_jpegs_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = EvidenceRecorder(storage_dir=tmp)
            frames = [(i * 100, _frame(i)) for i in range(5)]
            result = recorder.record(
                'cam-1', 'alert-1', frames, {'kind': 'person_entered_zone'})

            directory = Path(result['path'])
            jpgs = sorted(directory.glob('frame_*.jpg'))
            self.assertEqual(len(jpgs), 5, 'harus 5 file JPEG')
            for path in jpgs:
                self.assertGreater(path.stat().st_size, 0)
                self.assertTrue(path.read_bytes().startswith(b'\xff\xd8'),
                                'JPEG magic bytes')
            self.assertTrue((directory / 'metadata.json').is_file())
            self.assertEqual(result['file_count'], 5)
            self.assertEqual(result['duration_span_ms'], 400)

    def test_record_creates_missing_storage_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / 'deep' / 'nested' / 'evidence'
            recorder = EvidenceRecorder(storage_dir=nested)
            self.assertTrue(recorder.enabled)
            self.assertTrue(nested.is_dir(), 'storage dir harus dibuat otomatis')
            result = recorder.record('cam-1', 'alert-1', [(0, _frame(1))])
            self.assertIsNotNone(result)

    def test_sequential_filenames_and_metadata_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = EvidenceRecorder(storage_dir=tmp)
            frames = [(i * 50, _frame(i)) for i in range(5)]
            recorder.record('cam-9', 'alert-9', frames)

            directory = Path(tmp) / 'cam-9' / 'alert-9'
            names = sorted(p.name for p in directory.glob('frame_*.jpg'))
            self.assertEqual(names, [f'frame_{i:03d}.jpg' for i in range(5)])

            metadata = json.loads((directory / 'metadata.json').read_text())
            self.assertEqual(metadata['camera_id'], 'cam-9')
            self.assertEqual(metadata['alert_id'], 'alert-9')
            self.assertEqual(metadata['span'], 200)

    def test_list_and_serve(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = EvidenceRecorder(storage_dir=tmp)
            recorder.record('cam-1', 'alert-1', [(0, _frame(0))])

            listing = recorder.list_for_alert('alert-1')
            self.assertEqual(len(listing), 1)
            self.assertEqual(listing[0]['camera_id'], 'cam-1')
            self.assertEqual(len(listing[0]['files']), 1)

            served = recorder.serve('alert-1')
            self.assertEqual(len(served), 1)
            name, body = served[0]
            self.assertEqual(name, 'frame_000.jpg')
            self.assertTrue(body.startswith(b'\xff\xd8'))

            via_helper = get_evidence_files('alert-1', storage_dir=tmp)
            self.assertEqual(len(via_helper), 1)


@unittest.skipUnless(HAS_CV2, 'Requires cv2 + numpy (requirements-ml)')
class RingBufferTests(unittest.TestCase):
    def _manager(self, storage_dir):
        manager = CameraManager()
        manager._evidence_recorder = EvidenceRecorder(storage_dir=storage_dir)
        manager._evidence_resolved = True
        return manager

    def test_ring_buffer_keeps_only_last_30_of_40(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(tmp)
            ring = deque(maxlen=RING_BUFFER_FRAMES)
            for index in range(40):
                ring.append((index * 1000, _frame(index)))
            manager._ring['cam-1'] = ring

            result = manager.evidence_burst('cam-1', 'alert-1')
            self.assertIsNotNone(result)
            self.assertEqual(result['file_count'], RING_BUFFER_FRAMES)
            directory = Path(result['path'])
            jpgs = sorted(directory.glob('frame_*.jpg'))
            self.assertEqual(len(jpgs), RING_BUFFER_FRAMES)
            names = [p.name for p in jpgs]
            self.assertEqual(names[0], 'frame_000.jpg')
            self.assertEqual(names[-1], f'frame_{RING_BUFFER_FRAMES - 1:03d}.jpg')
            # deque(maxlen=30) membuang 10 frame pertama → span 10s..39s
            self.assertEqual(result['duration_span_ms'], 29000)

    def test_empty_ring_returns_none_without_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(tmp)
            self.assertNotIn('cam-1', manager._ring)
            result = manager.evidence_burst('cam-1', 'alert-empty')
            self.assertIsNone(result)
            self.assertFalse((Path(tmp) / 'cam-1' / 'alert-empty').exists())


if __name__ == '__main__':
    unittest.main()