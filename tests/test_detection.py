import contextlib
import io
import sys
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Sources.detection.model import DEFAULT_CLASSES, DetectionModel, detections_from_result

NAMES = {0: 'excavator', 1: 'excavator_arm', 2: 'person'}


def fake_result(*rows):
    boxes = [SimpleNamespace(cls=cls, conf=conf, xyxy=[[x1, y1, x2, y2]])
             for cls, conf, (x1, y1, x2, y2) in rows]
    return SimpleNamespace(boxes=boxes, names=NAMES)


EXCAVATOR = (0, 0.821, (10, 20, 300, 200))
ARM = (1, 0.492, (50, 30, 90, 160))
PERSON = (2, 0.912, (400, 150, 420, 230))


class DetectionMappingTests(unittest.TestCase):
    def test_mapping_keeps_all_fields(self):
        detections = detections_from_result(fake_result(EXCAVATOR, PERSON))
        self.assertEqual(len(detections), 2)
        excavator, person = detections
        self.assertEqual(excavator['label'], 'excavator')
        self.assertEqual(excavator['classId'], 0)
        self.assertEqual(excavator['confidence'], 0.821)
        self.assertEqual(excavator['box'], (10, 20, 300, 200))
        self.assertEqual(person['label'], 'person')

    def test_allowed_filter_hides_arm(self):
        detections = detections_from_result(fake_result(EXCAVATOR, ARM, PERSON), allowed=(0, 2))
        self.assertEqual([d['label'] for d in detections], ['excavator', 'person'])


class DetectionModelTests(unittest.TestCase):
    def test_without_model_runtime_stays_none_and_detect_is_empty(self):
        with contextlib.redirect_stdout(io.StringIO()):
            # Isolasi dari Server/.env dan model bawaan repo agar test tidak memuat ML.
            with unittest.mock.patch('Sources.detection.model.env_value', lambda key, default=None: default), \
                 unittest.mock.patch('Sources.detection.model.DEFAULT_MODEL_PATH', '/tidak/ada/model.pt'):
                model = DetectionModel()
        self.assertIsNone(model.runtime)
        self.assertEqual(model.classes, DEFAULT_CLASSES)
        self.assertEqual(model.detect(object()), ())

    def test_config_values_from_environment(self):
        values = {'DETECTION_MODEL_PATH': '', 'DETECTION_CONF': '0.5',
                   'DETECTION_IMGSZ': '960', 'DETECTION_CLASSES': '0,1,2'}
        with contextlib.redirect_stdout(io.StringIO()):
            with unittest.mock.patch('Sources.detection.model.env_value', values.get), \
                 unittest.mock.patch('Sources.detection.model.DEFAULT_MODEL_PATH', '/tidak/ada/model.pt'):
                model = DetectionModel()
        self.assertEqual(model.conf, 0.5)
        self.assertEqual(model.imgsz, 960)
        self.assertEqual(model.classes, (0, 1, 2))
        self.assertIsNone(model.runtime)

    def test_missing_file_keeps_detection_off(self):
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            model = DetectionModel(model_path='/tidak/ada/best.pt')
        self.assertIsNone(model.runtime)
        self.assertIn('tidak ditemukan', captured.getvalue())


import threading
import time


class _GuardedBox:
    """Box yang MENOLAK dibaca bila lock model tidak sedang dipegang —
    meniru tensor MPS yang tidak boleh disentuh di luar serialisasi."""

    def __init__(self, lock, cls, conf, xyxy):
        self._lock, self._inner = lock, SimpleNamespace(cls=cls, conf=conf, xyxy=[xyxy])

    def __getattr__(self, name):
        assert self._lock.locked(), 'akses box di luar lock inference'
        return getattr(self._inner, name)


class MpsSerializationTests(unittest.TestCase):
    """pytorch#197805: dua thread MPS bersamaan = SIGABRT Metal. Konversi hasil
    predict (float()/int() pada box) wajib terjadi bersama predict di dalam
    lock — kejadian lapangan 14 Nov 2026 (crash Thread 2 vs Thread 3)."""

    def _model_with(self, runtime):
        model = DetectionModel(model_path='/tidak/ada/best.pt')   # runtime tetap None
        model._infer_lock = threading.Lock()
        model.runtime = runtime
        return model

    def test_conversion_happens_inside_infer_lock(self):
        lock = threading.Lock()
        box = _GuardedBox(lock, cls=0, conf=0.912, xyxy=[10, 20, 300, 200])
        result = SimpleNamespace(boxes=[box], names=NAMES)
        runtime = SimpleNamespace(
            predict=lambda frame, **kwargs: (lock.acquire(), [result])[1])
        model = self._model_with(runtime)
        with contextlib.redirect_stdout(io.StringIO()):
            detections = model.detect(object())
        self.assertEqual(detections[0]['label'], 'excavator')
        lock.release()

    def test_detect_serializes_concurrent_threads(self):
        state = {'inside': 0, 'peak': 0}
        model = self._model_with(None)          # lock model dibuat dulu…

        class Runtime:
            def __init__(self, lock):
                self.lock = lock

            def predict(self, frame, **kwargs):
                state['inside'] += 1
                state['peak'] = max(state['peak'], state['inside'])
                time.sleep(0.01)
                state['inside'] -= 1
                box = _GuardedBox(self.lock, cls=2, conf=0.5, xyxy=[1, 2, 3, 4])
                return [SimpleNamespace(boxes=[box], names=NAMES)]

        model.runtime = Runtime(model._infer_lock)   # …lalu dipakai Runtime
        errors = []

        def worker():
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    for _ in range(10):
                        model.detect(object())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(state['peak'], 1, 'detect wajib serial — tak boleh 2 thread MPS bersamaan')


if __name__ == '__main__':
    unittest.main()
