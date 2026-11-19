"""Unit tests zona ground-plane meter (core/zones).

Kalibrasi dipangku duck-type: FakeCalib memetakan to_meter(x, y) =
(x/100, y/100) — jarak piksel/100 = jarak meter, deterministik tanpa numpy.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Sources.core import zones


class FakeCalib:
    def to_meter(self, x, y):
        return (x / 100, y / 100)


class BehindCameraCalib:
    def to_meter(self, x, y):
        return None


def det(cid, box):
    return {'label': 'x', 'classId': cid, 'confidence': 0.9, 'box': box}


EXCAVATOR = det(0, (100, 28, 500, 348))
# Titik tanah excavator (foot − 15% tinggi, aset v4): (300, 348 − 0.15*320)
# = (300, 300) -> (3.0, 3.0) m — sengaja dipilih supaya ekspektasi jarak lama
# tetap presisi (anchor 12 Nov, menggantikan uji pusat-65% sebelumnya).


def person_at(x_ground):                     # titik tanah (x_ground, 300)
    return det(2, (x_ground - 10, 260, x_ground + 10, 300))


class GroundPointTests(unittest.TestCase):
    def test_ground_point_is_bottom_center(self):
        self.assertEqual(zones.ground_point(det(2, (10, 20, 30, 40))), (20, 40))


class MeterZoneTests(unittest.TestCase):
    def test_default_4m_boundary_is_inclusive(self):
        near = zones.persons_within_radius([person_at(700)], [EXCAVATOR], FakeCalib(),
                                           zones.DEFAULT_ZONE_RADIUS_M)
        self.assertEqual(near, 1)  # jarak tepat 4.0 m termasuk di dalam

    def test_outside_default_radius_not_counted(self):
        far = zones.persons_within_radius([person_at(710)], [EXCAVATOR], FakeCalib(), 4.0)
        self.assertEqual(far, 0)  # 4.1 m

    def test_custom_radius_option(self):
        self.assertEqual(zones.persons_within_radius([person_at(690)], [EXCAVATOR], FakeCalib(), 2.0), 0)   # 3.9 m vs radius 2
        self.assertEqual(zones.persons_within_radius([person_at(490)], [EXCAVATOR], FakeCalib(), 2.0), 1)   # 1.9 m

    def test_person_behind_camera_skipped(self):
        self.assertEqual(zones.persons_within_radius([person_at(300)], [EXCAVATOR], BehindCameraCalib(), 4.0), 0)

    def test_all_excavator_points_behind_camera_zone_empty(self):
        self.assertEqual(zones.persons_within_radius([person_at(300)], [EXCAVATOR], BehindCameraCalib(), 4.0), 0)
        self.assertEqual(zones.persons_within_radius([person_at(300)], [], FakeCalib(), 4.0), 0)

    def test_multiple_excavators_any_zone_counts(self):
        second = det(0, (6900, 100, 7300, 300))  # titik tanah (7100,300) -> (71,3) m
        # person 1.9 m dari excavator pertama; excavator kedua 60+ m jauhnya —
        # cukup satu zona memuat person untuk terhitung (radius 2 m).
        self.assertEqual(
            zones.persons_within_radius([person_at(490)], [EXCAVATOR, second], FakeCalib(), 2.0), 1)

    def test_mixed_excavators_valid_and_unmeasurable(self):
        # Excavator valid tetap menilai person meski excavator lain tak terukur.
        unmeasurable = det(0, (0, 0, 0, 0))
        self.assertEqual(
            zones.persons_within_radius([person_at(400)], [EXCAVATOR, unmeasurable],
                                        MixedCalib(), 1.2), 1)              # person 1.0 m dari excavator valid

    def test_all_excavators_unmeasurable_counts_zero_even_huge_radius(self):
        unmeasurable = det(0, (0, 0, 0, 0))
        self.assertEqual(
            zones.persons_within_radius([person_at(400)], [unmeasurable, unmeasurable],
                                        MixedCalib(), 1000.0), 0)

    def test_huge_radius_counts_everyone(self):
        far_away = person_at(90000)              # (900,3) m
        self.assertEqual(
            zones.persons_within_radius([person_at(490), far_away], [EXCAVATOR], FakeCalib(), 1000.0), 2)


class MixedCalib:
    """Kamera gagal memproyeksikan hanya titik (0,0) — lainnya terukur."""

    def to_meter(self, x, y):
        if (x, y) == (0.0, 0.0):
            return None
        return (x / 100, y / 100)


class HighRiskZoneTests(unittest.TestCase):
    """Zona high-risk ½ radius (keputusan owner 8-9 Nov 2026).

    EXCAVATOR titik tanah (3.0, 3.0) m; person_at(x_ground) berjarak
    |x_ground/100 - 3.0| meter.
    """

    def test_high_risk_radius_is_half(self):
        self.assertEqual(zones.high_risk_radius(4.0), 2.0)
        self.assertEqual(zones.high_risk_radius(10.0), 5.0)
        self.assertEqual(zones.high_risk_radius(8.0, 0.25), 2.0)

    def test_default_high_risk_ratio_const(self):
        self.assertEqual(zones.DEFAULT_HIGH_RISK_RATIO, 0.5)
        self.assertEqual(zones.high_risk_radius(zones.DEFAULT_ZONE_RADIUS_M),
                         2.0)

    def test_persons_within_high_risk_boundary(self):
        # radius high-risk 2 m: 1.9 & tepat 2.0 m dihitung, 2.1 m tidak.
        self.assertEqual(zones.persons_within_high_risk(
            [person_at(490)], [EXCAVATOR], FakeCalib(), 2.0), 1)   # 1.9 m
        self.assertEqual(zones.persons_within_high_risk(
            [person_at(500)], [EXCAVATOR], FakeCalib(), 2.0), 1)   # 2.0 m (inklusif)
        self.assertEqual(zones.persons_within_high_risk(
            [person_at(510)], [EXCAVATOR], FakeCalib(), 2.0), 0)   # 2.1 m

    def test_zone_status_danger_warning_clear(self):
        danger = zones.zone_status_for_unit([person_at(450)], [EXCAVATOR], FakeCalib())
        self.assertEqual(danger, 'danger')                          # 1.5 m <= 2 m
        warning = zones.zone_status_for_unit([person_at(600)], [EXCAVATOR], FakeCalib())
        self.assertEqual(warning, 'warning')                        # 3 m (dalam 4 m, luar 2 m)
        cleared = zones.zone_status_for_unit([person_at(1000)], [EXCAVATOR], FakeCalib())
        self.assertEqual(cleared, 'clear')                          # 7 m > 6 m

    def test_zone_status_band_holds_warning_until_clear(self):
        # band histeresis 4–6 m tetap 'warning' (zona aktif); > 6 m baru 'clear'.
        band = zones.zone_status_for_unit([person_at(710)], [EXCAVATOR], FakeCalib())
        self.assertEqual(band, 'warning')                           # 4.1 m

    def test_zone_status_no_person_is_clear(self):
        self.assertEqual(zones.zone_status_for_unit([], [EXCAVATOR], FakeCalib()), 'clear')
        self.assertEqual(zones.zone_status_for_unit([person_at(300)], [], FakeCalib()), 'clear')

    def test_zone_status_behind_camera_is_clear(self):
        self.assertEqual(
            zones.zone_status_for_unit([person_at(300)], [EXCAVATOR], BehindCameraCalib()),
            'clear')


if __name__ == '__main__':
    unittest.main()
