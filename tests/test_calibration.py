"""Test kalibrasi ground-plane GroundCalibrationV2 (core/calibration).

Pendekatan: kamera pinhole sintetis dengan ground-truth diketahui — bangun
proyeksi world→piksel sendiri, kalibrasi GroundCalibrationV2 darinya, lalu
bandingkan hasil to_meter() dengan koordinat meter sebenarnya.
"""
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import numpy as np

    from Sources.core.calibration import CalibrationError, GroundCalibrationV2

    HAS_NUMPY = True
except ImportError:  # numpy bagian requirements-ml.txt (opsional)
    HAS_NUMPY = False


# ---------------- Kamera pinhole sintetis (ground-truth) ----------------
FX = FY = 700.0
K = np.array([[FX, 0, 640.0], [0, FY, 360.0], [0, 0, 1]]) if HAS_NUMPY else None
CAM_POS = np.array([0.0, -14.0, 6.0]) if HAS_NUMPY else None
CAM_TARGET = np.array([0.0, 10.0, 0.0]) if HAS_NUMPY else None

if HAS_NUMPY:
    _fwd = CAM_TARGET - CAM_POS
    _fwd /= np.linalg.norm(_fwd)
    _right = np.cross(_fwd, np.array([0.0, 0.0, 1.0]))
    _right /= np.linalg.norm(_right)
    _down = np.cross(_right, _fwd)
    ROT = np.stack([_right, _down, _fwd], axis=0)


def project(X, Y, Z=0.0):
    """Proyeksi titik world ground-plane ke piksel (ground-truth oracle)."""
    pc = ROT @ (np.array([X, Y, Z]) - CAM_POS)
    if pc[2] <= 1e-9:
        return None
    uv = K @ (pc / pc[2])
    return float(uv[0]), float(uv[1])


CAL_M = [(0, 0), (10, 0), (0, 10), (10, 10), (5, 5), (-3, 12), (12, 4)]
CAL_PX = [project(x, y) for x, y in CAL_M]


@unittest.skipUnless(HAS_NUMPY, 'butuh numpy (requirements-ml.txt)')
class GroundCalibrationV2Tests(unittest.TestCase):

    def make_calibration(self):
        self.assertTrue(all(p is not None for p in CAL_PX))
        return GroundCalibrationV2(CAL_PX, CAL_M)

    def make_grid(self, n=200):
        rng = np.random.default_rng(42)
        xs = rng.uniform(-6, 13, n)
        ys = rng.uniform(-1, 17, n)
        for X, Y in zip(xs, ys):
            px = project(X, Y)
            if px is not None:
                yield X, Y, px

    # ---------------- inti matematis ----------------

    def test_noise_free_fit_is_exact(self):
        gc = self.make_calibration()
        self.assertLess(gc.rms_fit, 1e-6)
        self.assertGreaterEqual(gc.check_health(), 0.0)

    def test_grid_accuracy_noise_free(self):
        gc = self.make_calibration()
        worst = 0.0
        for X, Y, px in self.make_grid():
            m = gc.to_meter(*px)
            self.assertIsNotNone(m)
            worst = max(worst, math.hypot(m[0] - X, m[1] - Y))
        self.assertLess(worst, 0.05, f'max grid error {worst:.4f} m')

    def test_grid_accuracy_with_click_noise(self):
        rng = np.random.default_rng(7)
        noisy = [(x + rng.uniform(-2, 2), y + rng.uniform(-2, 2))
                 for x, y in CAL_PX]
        gc = GroundCalibrationV2(noisy, CAL_M)
        errs = []
        for X, Y, px in self.make_grid():
            m = gc.to_meter(*px)
            if m is not None:
                errs.append(math.hypot(m[0] - X, m[1] - Y))
        mean = sum(errs) / len(errs)
        self.assertLess(mean, 0.35,
                        f'mean error {mean:.3f} m dengan noise klik ±2 px')

    # ---------------- holdout out-of-sample ----------------

    def test_holdout_is_true_out_of_sample(self):
        sick = (CAL_PX[6][0] + 40, CAL_PX[6][1] + 40)
        gc_pure = GroundCalibrationV2(CAL_PX[:6], CAL_M[:6])
        gc_h = GroundCalibrationV2(CAL_PX[:6] + [sick], CAL_M[:7])
        # H tidak boleh terpengaruh titik ekstra yang sakit
        self.assertTrue(np.allclose(gc_h.H, gc_pure.H, atol=1e-9))
        # rms_holdout = error langsung titik ekstra di bawah H yang murni
        m_sick = gc_pure.to_meter(*sick)
        direct = math.hypot(m_sick[0] - CAL_M[6][0], m_sick[1] - CAL_M[6][1])
        self.assertAlmostEqual(gc_h.rms_holdout, direct, places=6)

    # ---------------- kasus gagal ----------------

    def test_rejects_fewer_than_four_points(self):
        with self.assertRaises(CalibrationError):
            GroundCalibrationV2(CAL_PX[:3], CAL_M[:3])

    def test_rejects_count_mismatch(self):
        with self.assertRaises(CalibrationError):
            GroundCalibrationV2(CAL_PX, CAL_M[:5])

    def test_rejects_coincident_points(self):
        with self.assertRaises(CalibrationError):
            GroundCalibrationV2([(100.0, 100.0)] * 6, CAL_M[:6])

    def test_rejects_collinear_points_via_health_check(self):
        col_m = [(i * 2, 0) for i in range(6)]
        col_px = [project(x, y) for x, y in col_m]
        gc = GroundCalibrationV2(col_px, col_m)
        with self.assertRaises(CalibrationError):
            gc.check_health()

    # ---------------- perilaku proyeksi ----------------

    def test_pixel_meter_roundtrip(self):
        gc = self.make_calibration()
        worst = 0.0
        for X, Y, px in self.make_grid(50):
            m = gc.to_meter(*px)
            back = gc.to_pixel(*m)
            worst = max(worst, math.hypot(back[0] - px[0], back[1] - px[1]))
        self.assertLess(worst, 0.1, f'roundtrip max {worst:.4f} px')

    def test_to_pixel_behind_camera_returns_none(self):
        gc = self.make_calibration()
        # kamera berdiri di ground (0, -14); titik 4 m di belakangnya
        self.assertIsNone(project(0, -18))
        self.assertIsNone(gc.to_pixel(0, -18))

    def test_ground_circle_polygon_valid(self):
        gc = self.make_calibration()
        poly = gc.ground_circle_polygon((5, 5), 8.0, n_points=32)
        self.assertEqual(len(poly), 32)
        for x, y in poly:
            self.assertTrue(math.isfinite(x) and math.isfinite(y))

    def test_save_load_roundtrip(self):
        gc = self.make_calibration()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'calib.json'
            gc.save(path)
            loaded = GroundCalibrationV2.load(path)
        self.assertTrue(np.allclose(loaded.H, gc.H, atol=1e-12))
        m1, m2 = gc.to_meter(*CAL_PX[0]), loaded.to_meter(*CAL_PX[0])
        self.assertAlmostEqual(m1[0], m2[0], places=9)
        self.assertAlmostEqual(m1[1], m2[1], places=9)


if __name__ == '__main__':
    unittest.main()
