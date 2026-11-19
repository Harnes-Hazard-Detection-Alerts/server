"""Radius Safety Zone per excavator (api-draft 4.4a).

Evaluator memakai radius per unit lewat `radius_for`; mode piksel
menskalakan padding bbox sebanding radius (aproksimasi tanpa kalibrasi).
ZoneSettings menyimpan nilai ke database dan meng-cache-nya.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Sources.core.evaluator import RiskEvaluator
from Sources.equipment.zone import ZoneSettings


def detection(label, box):
    return {'label': label, 'classId': {'excavator': 0, 'person': 2}[label],
            'confidence': 0.9, 'box': box}


# Box 400x200 → padding default 0.25 → zona piksel x 0..600, y 50..350.
EXCAVATOR = detection('excavator', (100, 100, 500, 300))
EDGE_PERSON = detection('person', (560, 250, 580, 290))     # tengah x=570: dalam default
OUTSIDE_PERSON = detection('person', (640, 250, 660, 290))  # tengah x=650: luar default


def confirm(evaluator, person, source='cam12345-video'):
    evaluator.evaluate([EXCAVATOR, person], timestamp_ms=1, source_id=source, now_ms=10_000)
    return evaluator.evaluate([EXCAVATOR, person], timestamp_ms=2, source_id=source,
                              now_ms=10_300)


class PerUnitRadiusTests(unittest.TestCase):
    def test_default_radius_without_provider(self):
        evaluator = RiskEvaluator()
        self.assertEqual(confirm(evaluator, EDGE_PERSON)['kind'], 'person_entered_zone')
        self.assertIsNone(confirm(RiskEvaluator(), OUTSIDE_PERSON))

    def test_larger_unit_radius_widens_pixel_zone(self):
        evaluator = RiskEvaluator(radius_for=lambda eq: 8.0 if eq == 'cam12345-eq-1' else None)
        self.assertEqual(confirm(evaluator, OUTSIDE_PERSON)['kind'], 'person_entered_zone')

    def test_smaller_unit_radius_narrows_pixel_zone(self):
        evaluator = RiskEvaluator(radius_for=lambda eq: 2.0)
        self.assertIsNone(confirm(evaluator, EDGE_PERSON))

    def test_radius_is_looked_up_by_fallback_identity(self):
        seen = []
        evaluator = RiskEvaluator(radius_for=lambda eq: seen.append(eq))
        confirm(evaluator, EDGE_PERSON, source='abcdefgh-camera')
        self.assertIn('abcdefgh-eq-1', seen)

    def test_invalid_provider_value_falls_back_to_global(self):
        evaluator = RiskEvaluator(radius_for=lambda eq: 0)
        self.assertEqual(evaluator.unit_radius_m('x-eq-1'), evaluator.zone_radius_m)


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if sql.startswith('SELECT'):
            return FakeResult(list(self.db.items()))
        self.db[params[0]] = params[1]
        return FakeResult([])


class ZoneSettingsTests(unittest.TestCase):
    def setUp(self):
        self.db = {'cam1-eq-1': 6.0}
        self.settings = ZoneSettings(4.0, connection_factory=lambda: FakeConnection(self.db))

    def test_load_and_item(self):
        self.assertTrue(self.settings.load())
        self.assertEqual(self.settings.radius_for('cam1-eq-1'), 6.0)
        item = self.settings.item('cam1-eq-1')
        self.assertEqual(item['zoneRadiusM'], 6.0)
        self.assertTrue(item['custom'])

    def test_unknown_equipment_uses_default(self):
        item = self.settings.item('other-eq-1')
        self.assertEqual(item['zoneRadiusM'], 4.0)
        self.assertFalse(item['custom'])
        self.assertIsNone(self.settings.radius_for('other-eq-1'))

    def test_set_persists_and_updates_cache(self):
        item = self.settings.set('cam2-eq-1', 8, None, 123)
        self.assertEqual(self.db['cam2-eq-1'], 8.0)
        self.assertEqual(self.settings.radius_for('cam2-eq-1'), 8.0)
        self.assertEqual(item['zoneRadiusM'], 8.0)

    def test_set_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            self.settings.set('cam2-eq-1', 11, None, 123)
        with self.assertRaises(ValueError):
            self.settings.set('cam2-eq-1', 1.5, None, 123)


if __name__ == '__main__':
    unittest.main()
