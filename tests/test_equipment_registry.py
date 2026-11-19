"""Jembatan registry→katalog equipment (DB-gated, keputusan owner 13 Nov 2026).

create_from_camera dan update_profile harus menjaga
equipment_instances (katalog identitas + FK assignments, migrasi 013/014)
tetap sinkron dengan registry equipment_cameras + equipment_labels — kalau
tidak, skenario REAL (unit dibuat HSE lewat app) ditolak not_found saat
assign karena tidak ada alur lain yang mengisi katalog.
"""
import os
import sys
import time
import unittest
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

PASSWORD = 'temporary-test-password'


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1',
                     'Requires local PostgreSQL')
class EquipmentRegistryBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from Sources.infrastructure.database.connection import connect, database_url
        from Sources.infrastructure.database.migrate import migrate
        from Sources.users.create import create_user

        cls.admin = psycopg.connect('dbname=postgres host=/tmp', autocommit=True)
        name = 'safety_registry_test_' + uuid4().hex
        cls.admin.execute(sql.SQL('CREATE DATABASE {} OWNER safety_app').format(sql.Identifier(name)))

        def factory():
            return connect(make_conninfo(database_url(), dbname=name))
        cls.factory = staticmethod(factory)
        with factory() as connection:
            migrate(connection)
        cls.hse_id = create_user('reg-hse', 'HSE Reg', 'hse', PASSWORD, factory)
        cls.op_id = create_user('reg-op', 'Op Reg', 'operator', PASSWORD, factory)

        def stop_and_drop():
            cls.admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
            cls.admin.close()
        cls.addClassCleanup(stop_and_drop)

    def _camera(self):
        """Satu kamera RTSP dummy (tanpa file/cv2) + id-nya."""
        from Sources.cameras import service
        return service.create_camera('Cam Reg', 'Tes', 'rtsp',
                                     f'rtsp://127.0.0.1:8554/{uuid4().hex}',
                                     None, False, int(time.time() * 1000),
                                     connection_factory=self.factory)

    def _catalog_name(self, equipment_id):
        with self.factory() as connection:
            row = connection.execute(
                'SELECT name FROM equipment_instances '
                'WHERE equipment_instance_id=%s', (equipment_id,)).fetchone()
        return row[0] if row else None

    def test_create_from_camera_mengisi_katalog_dan_assignment_lolos(self):
        """Alur scenario real: unit dibuat dari kamera → assign operator
        langsung boleh — katalog equipment_instances ikut terisi."""
        from Sources.assignments.service import AssignmentError, create_assignment
        from Sources.equipment import registry

        camera = self._camera()
        unit = registry.create_from_camera(camera['id'], 'Excavator Depo 01',
                                           self.hse_id, int(time.time() * 1000),
                                           connection_factory=self.factory)
        self.assertEqual(unit['name'], 'Excavator Depo 01')
        # Katalog terisi (FK assignments → equipment_instances puas).
        self.assertEqual(self._catalog_name(unit['equipmentInstanceId']),
                         'Excavator Depo 01')
        # Assignment — semula not_found di sini (bug merge-union 13 Nov).
        item = create_assignment(unit['equipmentInstanceId'], self.op_id,
                                 int(time.time() * 1000),
                                 connection_factory=self.factory)
        self.assertEqual(item['status'], 'active')
        self.assertEqual(item['equipmentName'], 'Excavator Depo 01')

    def test_update_profile_menyinkronkan_nama_katalog(self):
        """Ganti nama unit lewat HSE → katalog ikut, listing assignment
        tidak menampilkan nama basi."""
        from Sources.equipment import labels, registry

        camera = self._camera()
        unit = registry.create_from_camera(camera['id'], 'Nama Awal',
                                           self.hse_id, int(time.time() * 1000),
                                           connection_factory=self.factory)
        labels.update_profile(unit['equipmentInstanceId'], {'name': 'Nama Baru'},
                              self.hse_id, int(time.time() * 1000),
                              connection_factory=self.factory)
        self.assertEqual(self._catalog_name(unit['equipmentInstanceId']),
                         'Nama Baru',
                         'katalog harus ikut mutakhir saat nama diganti')
        from Sources.assignments.service import list_assignments
        names = [item['equipmentName'] for item
                 in list_assignments(connection_factory=self.factory)]
        self.assertNotIn('Nama Awal', names)


if __name__ == '__main__':
    unittest.main()
