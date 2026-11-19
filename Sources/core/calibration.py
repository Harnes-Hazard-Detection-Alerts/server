"""Kalibrasi ground-plane piksel ↔ meter (core).

Homografi bidang tanah hasil pembangunan-ulang dari nol (versi V2), dipakai
untuk mengubah titik tanah (kaki person / titik kontak excavator) dari piksel
menjadi koordinat meter sehingga jarak bisa dihitung secara fisik.

Kelas dan alur:
    GroundCalibrationV2 — DLT homography dengan Hartley normalization.
      * Minimal 4 pasangan (piksel, meter); rekomendasi ≥6 agar tersedia
        RMS holdout yang benar (H difit hanya dari titik pertama; titik
        ekstra = evaluasi out-of-sample).
      * check_health() menolak kalibrasi dengan RMS holdout > max_rms
        (default 0.15 m) atau condition number > max_cond (default 1e6),
        contoh titik yang nyaris kolinear.

Perilaku penting (divalidasi sintetis pinhole 2026-11-07):
    * noise-free  : error grid 0 m (presisi mesin).
    * noise ±2 piksel pada titik kalibrasi: rata-rata ~0.12 m, p95 ~0.29 m.
    * to_pixel() mengembalikan None untuk titik meter di belakang kamera
      (W ≤ 0) — mencegah polygon zona terdistorsi saat excavator dekat kamera.

Catatan dependensi: modul ini butuh numpy (bagian requirements-ml.txt).
Evaluator zona meter memakai instance ini lewat injeksi (duck-typed
`to_meter`/`to_pixel`) — evaluator tidak mengimpor modul ini, sehingga
server tetap hidup tanpa requirements-ml.txt selama kalibrasi tidak dipakai.
"""
import json
import math
from pathlib import Path

try:
    import numpy as np
except ImportError:  # server inti jalan tanpa requirements-ml.txt
    np = None  # type: ignore


def _require_numpy():
    if np is None:
        raise CalibrationError(
            'numpy belum terpasang (pip install -r requirements-ml.txt); '
            'kalibrasi meter tidak tersedia — server tetap jalan mode piksel.')
    return np


class CalibrationError(Exception):
    """Kalibrasi gagal dibangun atau gagal health-check."""


class GroundCalibrationV2:
    """Kalibrasi ground plane dari pasangan (piksel, meter).

    Hartley-normalized DLT → least squares → RMS holdout + condition number.
    """

    def __init__(self, pixel_points, meter_points):
        _require_numpy()
        px = np.asarray(pixel_points, dtype=np.float64)
        mt = np.asarray(meter_points, dtype=np.float64)
        if px.shape[0] < 4:
            raise CalibrationError('minimal 4 titik (rekomendasi ≥6 untuk RMS valid)')
        if px.shape[0] != mt.shape[0]:
            raise CalibrationError('jumlah pixel dan meter tidak sama')

        n = px.shape[0]
        self.pixel_points = px
        self.meter_points = mt

        # Holdout scheme: >6 titik → H difit dari 6 PERTAMA saja,
        # sisanya (n-6) jadi evaluasi out-of-sample yang benar.
        if n > 6:
            fit_idx = list(range(6))
            hold_idx = list(range(6, n))
        else:
            fit_idx = list(range(n))
            hold_idx = []

        # Hartley normalization: translate + scale (isotropic) — pada titik fit
        px_c, px_T = self._hartley_normalize(px[fit_idx])
        mt_c, mt_T = self._hartley_normalize(mt[fit_idx])

        # Matriks A (2×n_fit × 9) — homogeneous DLT
        A = self._build_A(px_c, mt_c)

        # SVD → vektor singular kanan terakhir (ruang null)
        _, _, Vt = np.linalg.svd(A)
        h = Vt[-1]
        H_norm = h.reshape(3, 3)

        # Denormalisasi + normalisasi h33 = 1
        H = np.linalg.inv(mt_T) @ H_norm @ px_T
        if H[2, 2] != 0:
            H = H / H[2, 2]
        self.H = H

        # RMS fit (semua titik; H hanya difit dari fit_idx)
        self.rms_fit = self._rms_static(px, mt, H, list(range(n)))
        # RMS holdout: titik ekstra — benar-benar out-of-sample
        self.rms_holdout = (self._rms_static(px, mt, H, hold_idx)
                            if hold_idx else self.rms_fit)

        # Condition number dari H
        self.condition = np.linalg.cond(H)

    @staticmethod
    def _hartley_normalize(pts):
        """Translate centroid ke origin, scale jarak rata-rata ke √2."""
        _require_numpy()
        c = pts.mean(axis=0)
        d = np.sqrt(((pts - c) ** 2).sum(axis=1)).mean()
        if d < 1e-12:
            raise CalibrationError('semua titik berimpit')
        s = math.sqrt(2.0) / d
        T = np.array([[s, 0, -s * c[0]],
                      [0, s, -s * c[1]],
                      [0, 0, 1]])
        return (pts - c) * s, T

    @staticmethod
    def _build_A(px, mt):
        """Bangun matriks DLT 2n × 9."""
        _require_numpy()
        n = px.shape[0]
        A = np.zeros((2 * n, 9))
        for i in range(n):
            x, y = px[i]
            u, v = mt[i]
            A[2 * i] = [x, y, 1, 0, 0, 0, -x * u, -y * u, -u]
            A[2 * i + 1] = [0, 0, 0, x, y, 1, -x * v, -y * v, -v]
        return A

    @staticmethod
    def _rms_static(px, mt, H, idx):
        """RMS error proyeksi (meter) untuk subset titik."""
        _require_numpy()
        if not idx:
            return float('nan')
        errors = []
        for i in idx:
            p = np.array([px[i][0], px[i][1], 1])
            m = H @ p
            if abs(m[2]) < 1e-12:
                continue
            pred = m[:2] / m[2]
            errors.append(np.sqrt(((pred - mt[i]) ** 2).sum()))
        return float(np.sqrt(np.mean(np.array(errors) ** 2))) if errors else float('nan')

    def to_meter(self, x, y):
        """Piksel → meter. Return (X, Y) atau None bila W ≈ 0."""
        _require_numpy()
        p = np.array([x, y, 1])
        m = self.H @ p
        if abs(m[2]) < 1e-10:
            return None
        return float(m[0] / m[2]), float(m[1] / m[2])

    def to_pixel(self, X, Y):
        """Meter → piksel (inverse H). Return (x, y) atau None bila W ≤ 0
        (di belakang kamera / horizon) atau hasil di luar clamp ±10000 px."""
        _require_numpy()
        H_inv = np.linalg.inv(self.H)
        m = np.array([X, Y, 1])
        p = H_inv @ m
        if p[2] <= 1e-10:
            return None
        x, y = p[0] / p[2], p[1] / p[2]
        if x < -10000 or x > 10000 or y < -10000 or y > 10000:
            return None
        return float(x), float(y)

    def ground_circle_polygon(self, center_m, radius_m, n_points=32):
        """Lingkaran ground-plane (meter) → polygon piksel via to_pixel.

        Return list (x, y) piksel; titik yang return None di-skip
        (misal bagian lingkaran di belakang kamera).
        """
        cx, cy = center_m
        pts = []
        for i in range(n_points):
            angle = 2 * math.pi * i / n_points
            X = cx + radius_m * math.cos(angle)
            Y = cy + radius_m * math.sin(angle)
            px = self.to_pixel(X, Y)
            if px is not None:
                pts.append(px)
        return pts

    def check_health(self, max_rms=0.15, max_cond=1e6):
        """Tolak kalibrasi bila holdout RMS > max_rms atau cond > max_cond."""
        if self.condition > max_cond:
            raise CalibrationError(
                f'ill-conditioned H: cond={self.condition:.0f} > {max_cond}')
        rms = self.rms_holdout
        if rms > max_rms:
            raise CalibrationError(
                f'holdout RMS {rms:.3f}m > {max_rms}m — kalibrasi tidak akurat')
        return rms

    def save(self, path):
        """Simpan kalibrasi ke JSON (H, titik, RMS, condition)."""
        _require_numpy()
        data = {
            'H': self.H.tolist(),
            'pixel_points': self.pixel_points.tolist(),
            'meter_points': self.meter_points.tolist(),
            'rms_fit': self.rms_fit,
            'rms_holdout': self.rms_holdout,
            'condition': float(self.condition),
        }
        Path(path).write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path):
        """Muat kalibrasi dari file hasil save()."""
        _require_numpy()
        d = json.loads(Path(path).read_text())
        obj = cls.__new__(cls)
        obj.H = np.array(d['H'])
        obj.pixel_points = np.array(d['pixel_points'])
        obj.meter_points = np.array(d['meter_points'])
        obj.rms_fit = d['rms_fit']
        obj.rms_holdout = d['rms_holdout']
        obj.condition = d['condition']
        return obj


def load_healthy(path):
    """Muat calib JSON + health-check sekali jalan.

    Membalut GroundCalibrationV2.load + check_health untuk konsumen infra
    (server/ Tools): melempar OSError (file), ValueError (JSON rusak), atau
    CalibrationError (RMS holdout/cond tak layak) — fail-fast karena meter
    salah = alert salah diam-diam; konsumen memutuskan pesan keluar.
    """
    calibration = GroundCalibrationV2.load(path)
    calibration.check_health()
    return calibration


def load_camera_calibrations(directory, cameras):
    """Muat kalibrasi PER KAMERA dari folder (Server/calibrations/).

    File dinamai `{Nama Kamera}.json` (nama kamera = tabel cameras) dan berisi
    `pixel_points` yang SUDAH diskalakan ke resolusi video yang dipakai server
    (pembakuan oleh Tools/bake_calibrations.py — titik dan H homografi
    selaras dengan frame). Hanya kamera dengan file kalibrasi yang dapat
    mode meter; sisanya tetap zona piksel fallback. Kamera baru tanpa calib
    diabaikan (bukan error) — tambah file lalu restart server.

    Return {camera_id: GroundCalibrationV2} — kosong bila folder tak ada
    atau numpy tak terpasang (fallback piksel).
    """
    if np is None:
        return {}
    root = Path(directory)
    if not root.is_dir():
        return {}
    by_name = {camera['name']: camera['id'] for camera in cameras}
    loaded = {}
    for path in sorted(root.glob('*.json')):
        camera_id = by_name.get(path.stem)
        if camera_id is None:
            continue
        loaded[camera_id] = load_healthy(path)
    return loaded
