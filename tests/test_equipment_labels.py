"""Nama excavator (api-draft 4.4b) dan pemetaan equipment -> kamera."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Sources.cameras.service import camera_for_equipment
from Sources.equipment import labels


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self, db, log, instances=None):
        self.db, self.log = db, log
        self.instances = instances or {}  # katalog equipment_instances (jembatan 13 Nov)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        if sql.startswith('SELECT equipment_instance_id'):
            return FakeResult([(key, *value) for key, value in self.db.items()])
        if sql.startswith('SELECT display_name'):
            value = self.db.get(params[0])
            return FakeResult([value] if value else [])
        if sql.startswith('INSERT INTO equipment_instances'):
            self.instances[params[0]] = params[1]  # sinkron katalog oleh labels
            return FakeResult([])
        if sql.startswith('DELETE'):
            self.db.pop(params[0], None)
        elif sql.startswith('INSERT'):
            self.db[params[0]] = (params[1], params[2])
        return FakeResult([])


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.db, self.log, self.instances = {}, [], {}
        self.factory = lambda: FakeConnection(self.db, self.log, self.instances)

    def test_name_and_phone_are_independent(self):
        labels.update_profile('cam1-eq-1', {'name': 'EXC-01'}, None, 1,
                              connection_factory=self.factory)
        labels.update_profile('cam1-eq-1', {'cabinPhone': '+62811'}, None, 2,
                              connection_factory=self.factory)
        self.assertEqual(labels.get_profile('cam1-eq-1', connection_factory=self.factory),
                         {'name': 'EXC-01', 'cabinPhone': '+62811'})
        self.assertEqual(labels.get_label('cam1-eq-1', connection_factory=self.factory), 'EXC-01')
        self.assertEqual(labels.list_profiles(connection_factory=self.factory),
                         {'cam1-eq-1': {'name': 'EXC-01', 'cabinPhone': '+62811'}})

    def test_clearing_both_fields_deletes_profile(self):
        self.db['cam1-eq-1'] = ('EXC-01', None)
        labels.update_profile('cam1-eq-1', {'name': None}, None, 1,
                              connection_factory=self.factory)
        self.assertNotIn('cam1-eq-1', self.db)

    def test_clearing_name_keeps_phone(self):
        self.db['cam1-eq-1'] = ('EXC-01', '+62811')
        saved = labels.update_profile('cam1-eq-1', {'name': None}, None, 1,
                                      connection_factory=self.factory)
        self.assertEqual(saved, {'name': None, 'cabinPhone': '+62811'})


class CameraForEquipmentTests(unittest.TestCase):
    def test_non_fallback_id_skips_database(self):
        log = []
        factory = lambda: FakeConnection({}, log)
        self.assertIsNone(camera_for_equipment('EXC-01', connection_factory=factory))
        self.assertIsNone(camera_for_equipment('short-eq-1', connection_factory=factory))
        self.assertEqual(log, [])

    def test_fallback_id_queries_by_camera_prefix(self):
        log = []
        factory = lambda: FakeConnection({}, log)
        camera_for_equipment('4be4d068-eq-1', connection_factory=factory)
        self.assertEqual(log[0][1], ('4be4d068%',))


if __name__ == '__main__':
    unittest.main()
