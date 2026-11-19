import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import cv2
except ImportError:
    cv2 = None

if cv2 is None:
    raise unittest.SkipTest('opencv belum terpasang (requirements-ml.txt) — test reader diskip.')

from Sources.video_input.reader import RecordingReader


def make_video(path, frames=10, fps=25.0, size=(320, 240)):
    import numpy as np
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'mp4v'), fps, size)
    for index in range(frames):
        image = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        cv2.putText(image, str(index), (10, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 2)
        writer.write(image)
    writer.release()


class RecordingReaderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.video = Path(self._tmp.name) / 'sample.mp4'
        make_video(self.video, frames=10, fps=25.0)

    def tearDown(self):
        self._tmp.cleanup()

    def test_yields_every_frame_with_rising_timestamps(self):
        reader = RecordingReader(self.video)
        got = list(reader.frames())
        reader.release()
        self.assertEqual(len(got), 10)
        for (index, ts, frame) in got:
            self.assertEqual(frame.shape[:2], (240, 320))
        self.assertTrue(all(got[i][1] < got[i + 1][1] for i in range(len(got) - 1)))
        self.assertEqual(got[0][1], 0)
        self.assertEqual(got[1][1], 40)  # 1/25 fps → 40 ms

    def test_target_fps_reduces_yielded_frames(self):
        reader = RecordingReader(self.video, target_fps=5.0)  # 25/5 → stride 5
        got = list(reader.frames())
        reader.release()
        self.assertEqual(len(got), 2)  # frame 0 dan 5

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            RecordingReader(self.video.with_name('tidak-ada.mp4'))


if __name__ == '__main__':
    unittest.main()
