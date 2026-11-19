"""A unit registered from an unused camera must immediately support assignment."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1', 'Requires local PostgreSQL')
class EquipmentRegistrationDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        from Sources.infrastructure.database.connection import connect, database_url
        from Sources.infrastructure.database.migrate import migrate
        from Sources.users.create import create_user
        cls.name = 'safety_registration_test_' + uuid4().hex
        cls.admin = psycopg.connect('dbname=postgres host=/tmp', autocommit=True)
        cls.addClassCleanup(cls.admin.close)
        cls.admin.execute(sql.SQL('CREATE DATABASE {} OWNER safety_app').format(sql.Identifier(cls.name)))
        cls.addClassCleanup(lambda: cls.admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(cls.name))))
        cls.factory = staticmethod(lambda: connect(make_conninfo(database_url(), dbname=cls.name)))
        with cls.factory() as connection:
            migrate(connection)
        cls.operator = create_user('registration-op', 'Test Operator', 'operator', 'temporary-test-password', cls.factory)
        cls.hse = create_user('registration-hse', 'Test HSE', 'hse', 'temporary-test-password', cls.factory)

    def test_new_unit_can_be_assigned_and_duplicate_preserves_original(self):
        from Sources.assignments.service import create_assignment
        from Sources.cameras.service import create_camera
        from Sources.equipment.registry import create_from_camera, EquipmentError
        now = time.time_ns() // 1_000_000
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / 'recording.mp4'
            video.write_bytes(b'test registration only')
            camera = create_camera('Test camera', None, 'recording', str(video), 4, True, now, self.factory)
            unit = create_from_camera(camera['id'], 'Test excavator', self.hse, now, self.factory)
            assignment = create_assignment(unit['equipmentInstanceId'], self.operator, now, self.factory)
            self.assertEqual(assignment['equipmentName'], 'Test excavator')
            self.assertEqual(assignment['operatorId'], self.operator)
            with self.assertRaises(EquipmentError):
                create_from_camera(camera['id'], 'Duplicate name', self.hse, now, self.factory)
            with self.factory() as connection:
                row = connection.execute('SELECT name FROM equipment_instances WHERE equipment_instance_id=%s', (unit['equipmentInstanceId'],)).fetchone()
            self.assertEqual(row, ('Test excavator',))

    def test_missing_camera_leaves_no_equipment(self):
        from Sources.equipment.registry import create_from_camera, EquipmentError
        camera = str(uuid4())
        with self.assertRaises(EquipmentError):
            create_from_camera(camera, 'Missing camera', self.hse, 1000, self.factory)
        with self.factory() as connection:
            row = connection.execute('SELECT name FROM equipment_instances WHERE equipment_instance_id=%s', (camera[:8] + '-eq-1',)).fetchone()
        self.assertIsNone(row)

    def test_duplicate_name_is_rejected_without_linking_camera(self):
        from Sources.cameras.service import create_camera
        from Sources.equipment.registry import create_from_camera, EquipmentError
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / 'recording.mp4'
            video.write_bytes(b'test registration only')
            cameras = []
            for i in range(2):
                source = Path(directory) / f'recording-{i}.mp4'
                source.write_bytes(video.read_bytes())
                cameras.append(create_camera('Name check camera ' + str(i), None, 'recording',
                                             str(source), 4, True, 1000, self.factory))
            unit = create_from_camera(cameras[0]['id'], 'EXC-NAME-CHECK', self.hse, 1000, self.factory)
            for name in ['EXC-NAME-CHECK', 'exc-name-check', '  Exc-Name-Check\n']:
                with self.subTest(name=name), self.assertRaises(EquipmentError) as error:
                    create_from_camera(cameras[1]['id'], name, self.hse, 1001, self.factory)
                self.assertEqual(error.exception.code, 'equipment_name_in_use')
            # Legacy units use their camera name when no display label exists.
            with self.factory() as connection:
                connection.execute('DELETE FROM equipment_labels WHERE equipment_instance_id=%s',
                                   (unit['equipmentInstanceId'],))
            with self.assertRaises(EquipmentError) as error:
                create_from_camera(cameras[1]['id'], '  NAME CHECK CAMERA 0 ', self.hse, 1002, self.factory)
            self.assertEqual(error.exception.code, 'equipment_name_in_use')
            with self.factory() as connection:
                linked = connection.execute('SELECT 1 FROM equipment_cameras WHERE camera_id=%s',
                                            (cameras[1]['id'],)).fetchone()
            self.assertIsNone(linked)
            created = create_from_camera(cameras[1]['id'], '  Distinct excavator  ', self.hse, 1003, self.factory)
            self.assertEqual(created['name'], 'Distinct excavator')

    def test_concurrent_registrations_cannot_share_a_name(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from Sources.cameras.service import create_camera
        from Sources.equipment.registry import create_from_camera, EquipmentError
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / 'recording.mp4'
            video.write_bytes(b'test registration only')
            cameras = []
            for i in range(2):
                source = Path(directory) / f'recording-{i}.mp4'
                source.write_bytes(video.read_bytes())
                cameras.append(create_camera('Concurrent camera ' + str(i), None, 'recording',
                                             str(source), 4, True, 1000, self.factory))
            barrier = Barrier(2)

            def register(camera):
                barrier.wait(timeout=10)
                try:
                    create_from_camera(camera['id'], 'Concurrent excavator', self.hse, 1000, self.factory)
                    return 'created'
                except EquipmentError as error:
                    return error.code

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(register, cameras))
            self.assertCountEqual(results, ['created', 'equipment_name_in_use'])
