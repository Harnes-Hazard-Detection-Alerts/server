"""Tes zona render v4 (keputusan owner 12 Nov 2026).

Menutup kontrak anchor + exclusion setelah kembali ke semantik prototype v4:
1. `excavator_foot` — titik zona excavator = bottom-center − 15% tinggi bbox
   (aset vision/pipeline.py EXCAVATOR_MARGIN_RATIO); evaluator meter dan
   render overlay memakai titik yang sama. Person tetap `ground_point`.
2. `mark_operators` — person >=80% areanya di dalam bbox excavator =
   operator: bukan bahaya — evaluator mengabaikannya (patokan ruler R1
   di playback ikut di-skip via tanda yang sama).
3. `calibration_for` — kalibrasi evaluator per-source; kamera tanpa calib
   kembali ke zona piksel meski kamera lain punya calib.
"""
import unittest

from Sources.core.detection import mark_operators
from Sources.core.evaluator import RiskEvaluator
from Sources.core.zones import excavator_foot, ground_point


class ScaledCalib:
    """Kalibrasi buatan: px/100 == meter (geometri fixture test_zones lama)."""

    def to_meter(self, x, y):
        if (x, y) == (0.0, 0.0):
            return None
        return (x / 100, y / 100)


def excavator():
    # foot − 15%: (300, 348 − 48) = (300, 300) -> (3.0, 3.0) m (ScaledCalib).
    return {'classId': 0, 'label': 'excavator', 'confidence': 0.9,
            'box': (100, 28, 500, 348)}


def person(box):
    return {'classId': 2, 'label': 'person', 'confidence': 0.9, 'box': box}


class ExcavatorFootTests(unittest.TestCase):
    def test_foot_is_bottom_center_minus_margin(self):
        foot = excavator_foot(excavator())
        self.assertEqual(foot, (300.0, 300.0))

    def test_foot_margin_ratio_matches_v4(self):
        from Sources.core.zones import EXCAVATOR_MARGIN_RATIO
        self.assertEqual(EXCAVATOR_MARGIN_RATIO, 0.15)  # aset v4

    def test_foot_ratio_parameterizable(self):
        foot = excavator_foot({'box': (0, 0, 100, 100)}, ratio=0.0)
        self.assertEqual(foot, (50.0, 100.0))

    def test_person_still_bottom_center(self):
        self.assertEqual(ground_point({'box': (10, 20, 30, 40)}), (20.0, 40.0))


class MarkOperatorTests(unittest.TestCase):
    def test_person_inside_excavator_is_operator(self):
        ex = excavator()
        cabin = person((180, 60, 220, 120))
        mark_operators([ex, cabin])
        self.assertTrue(cabin['operator'])
        self.assertNotIn('operator', ex)

    def test_person_at_threshold_boundary(self):
        ex = excavator()
        # Person 25 lebar: tepat 20/25 = 0.8 di dalam → operator (>= inklusif).
        straddler = person((95, 70, 120, 180))
        mark_operators([ex, straddler])
        self.assertTrue(straddler['operator'])

    def test_walker_beside_machine_is_not_operator(self):
        ex = excavator()
        beside = person((490, 300, 620, 380))  # overlap parsial < 0.8
        mark_operators([ex, beside])
        self.assertFalse(beside.get('operator'))

    def test_person_alone_never_operator(self):
        alone = person((0, 0, 40, 90))
        mark_operators([alone])
        self.assertFalse(alone.get('operator'))

    def test_excavator_arm_untouched(self):
        ex = excavator()
        arm = {'classId': 1, 'label': 'excavator_arm', 'confidence': 0.9,
               'box': (120, 40, 480, 330)}
        mark_operators([ex, arm])
        self.assertNotIn('operator', arm)


class EvaluatorV4Tests(unittest.TestCase):
    """Meter mode dengan ScaledCalib; foot excavator (3.0, 3.0) m."""

    def evaluate_all(self, evaluator, detections, source_id='cam-1', steps=10):
        alerts = []
        now = 0
        for _ in range(steps):
            alert = evaluator.evaluate(detections, timestamp_ms=now,
                                       source_id=source_id, now_ms=now)
            if alert is not None:
                alerts.append((alert['kind'], alert.get('personsInZone')))
            now += 500  # melewati confirm window + jeda
        return alerts

    def evaluator(self, **kwargs):
        return RiskEvaluator(calibration=ScaledCalib(),
                             operating_for=lambda _id: True, **kwargs)

    def test_operator_inside_cabin_excluded(self):
        ex = excavator()
        operator = person((250, 150, 320, 240))  # di dalam bbox: operator
        mark_operators([ex, operator])
        alerts = self.evaluate_all(self.evaluator(), [ex, operator])
        self.assertEqual(alerts, [])

    def test_worker_in_zone_triggers_alert(self):
        ex = excavator()
        # Kaki (600, 300) -> (6.0, 3.0) m: 3 m dari foot — dalam zona 4 m.
        worker = person((590, 260, 610, 300))
        operator = person((250, 150, 320, 240))
        mark_operators([ex, worker, operator])
        alerts = self.evaluate_all(self.evaluator(), [ex, worker, operator])
        kinds = [kind for kind, _ in alerts]
        self.assertIn('person_entered_zone', kinds)
        entered = next(count for kind, count in alerts if kind == 'person_entered_zone')
        self.assertEqual(entered, 1)  # operator tak ikut terhitung

    def test_operator_high_risk_ignored(self):
        ex = excavator()
        operator = person((290, 280, 310, 300))  # ~0.1 m dari foot
        mark_operators([ex, operator])
        alerts = self.evaluate_all(self.evaluator(), [ex, operator])
        self.assertEqual(alerts, [])
        self.assertEqual(
            RiskEvaluator(calibration=ScaledCalib(),
                          operating_for=lambda _id: True)
            ._in_high_risk_count([ex], [operator], 4.0), 0)

    def test_per_source_calibration_pixel_fallback(self):
        evaluator = RiskEvaluator(
            calibration_for=lambda source_id: ScaledCalib() if source_id == 'calib-cam' else None,
            operating_for=lambda _id: True)
        ex = excavator()
        # Center person (650, 280) di luar bbox+25% piksel → plain-cam kosong.
        walker = person((640, 260, 660, 300))
        alerts = self.evaluate_all(evaluator, [ex, walker], source_id='plain-cam')
        self.assertEqual(alerts, [])
        # Calib-cam: kaki (650,300) -> (6.5, 3.0) = 3.5 m → dalam zona 4 m.
        alerts = self.evaluate_all(evaluator, [ex, walker], source_id='calib-cam')
        self.assertIn('person_entered_zone', [kind for kind, _ in alerts])

    def test_pixel_mode_operators_not_counted(self):
        evaluator = RiskEvaluator(operating_for=lambda _id: True)
        ex = excavator()
        operator = person((180, 60, 220, 120))
        walker = person((110, 300, 130, 320))  # seluruhnya dalam bbox = operator juga
        mark_operators([ex, operator, walker])
        alerts = self.evaluate_all(evaluator, [ex, operator, walker])
        self.assertNotIn('person_entered_zone', [kind for kind, _ in alerts])


if __name__ == '__main__':
    unittest.main()
