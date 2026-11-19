"""Tes kebijakan gerbang alert 12 Nov 2026 (akurasi alert).

Two changes under test, both DB-gated against a disposable database:
1. OperationStatus.reload — unit aktif bila assignment aktif + sesi operator
   hidup; binding hanya tombol "Stop" eksplisit (kasus dewi→Timur: pindah
   assignment via API menghapus binding lama tanpa membuat baru — gate lama
   salah mematikan perlindungan).
2. alerts.store.resolve_stale_actives — boot hygiene menutup alert 'active'
   warisan supaya app operator tidak me-replay klaxon-nya.
"""
import os
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import unittest


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1',
                    'Set RUN_DATABASE_TESTS=1 with PostgreSQL running')
@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1', 'needs postgresql')
class GateAndHygieneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        from Sources.infrastructure.database.connection import database_url
        cls.admin = psycopg.connect('dbname=postgres host=/tmp connect_timeout=5', autocommit=True)
        cls.addClassCleanup(cls.admin.close)
        cls.name = 'safety_gate_' + uuid4().hex[:8]
        cls.admin.execute(sql.SQL('CREATE DATABASE {} OWNER safety_app').format(sql.Identifier(cls.name)))
        cls.addClassCleanup(cls.drop_database, cls.name)
        cls.url = make_conninfo(database_url(), dbname=cls.name)
        import psycopg as _p
        def factory():
            return _p.connect(cls.url, connect_timeout=5)
        cls.connect = staticmethod(factory)

    @classmethod
    def drop_database(cls, name):
        from psycopg import sql
        cls.admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))

    def setUp(self):
        import time
        from Sources.infrastructure.database.migrate import migrate
        with self.connect() as c:
            migrate(c)
        # Akun: operator wanita (dewi-like) + sesi hidup 365 hari.
        import time
        now_ms = int(time.time() * 1000)
        with self.connect() as c:
            op_id = c.execute(
                "INSERT INTO users(username, display_name, password_hash, role) "
                "VALUES (%s, 'Op', 'x', 'operator') RETURNING id",
                ('op-' + uuid4().hex[:8],)).fetchone()[0]
            session_hash = uuid4().hex + uuid4().hex[:32]  # 64 hex
            c.execute("INSERT INTO sessions(token_hash, user_id, expires_at) "
                      "VALUES (%s, %s, now() + interval '365 days')",
                      (session_hash, op_id))
            self.op_id = op_id
        self.now_ms = now_ms

    def tearDown(self):
        with self.connect() as c:
            c.execute("DELETE FROM shift_bindings")
            c.execute("DELETE FROM assignments")
            c.execute("DELETE FROM sessions")
            c.execute("DELETE FROM users")

    # ---- helpers ------------------------------------------------------------
    def register_equipment(self, equipment='ex-01-eq-1'):
        with self.connect() as c:
            c.execute("INSERT INTO equipment_instances(equipment_instance_id, name) "
                      "VALUES (%s, 'Test Excavator') ON CONFLICT DO NOTHING", (equipment,))

    def insert_assignment(self, equipment='ex-01-eq-1', status='active', op=None, ended=None):
        self.register_equipment(equipment)
        with self.connect() as c:
            c.execute("INSERT INTO assignments(equipment_instance_id, operator_id, "
                      "assigned_at_ms, status, ended_at_ms, ended_reason) "
                      "VALUES (%s, %s, %s, %s, %s, %s)",
                      (equipment, op or self.op_id, self.now_ms, status, ended,
                       'unassigned_by_hse' if ended else None))

    def insert_binding(self, equipment='ex-01-eq-1', opstart=None, op=None):
        with self.connect() as c:
            c.execute("INSERT INTO shift_bindings(operator_id, equipment_instance_id, "
                      "set_at_ms, operation_started_at_ms) VALUES (%s, %s, %s, %s)",
                      (op or self.op_id, equipment, self.now_ms, opstart))

    def gate(self):
        from Sources.equipment.operation import OperationStatus
        status = OperationStatus(self.connect)
        status.reload()
        return status

    # ---- gate ----------------------------------------------------------------
    def test_assignment_plus_session_activates_without_binding(self):
        """Kasus dewi→Timur: assignment + sesi hidup, tanpa binding → AKTIF."""
        self.insert_assignment()
        self.assertTrue(self.gate().is_active('ex-01-eq-1'))

    def test_binding_without_start_is_explicit_stop(self):
        """Binding opstart NULL = operator menekan berhenti → nonaktif."""
        self.insert_assignment()
        self.insert_binding(opstart=None)
        self.assertFalse(self.gate().is_active('ex-01-eq-1'))

    def test_binding_with_start_activates(self):
        self.insert_assignment()
        self.insert_binding(opstart=self.now_ms)
        self.assertTrue(self.gate().is_active('ex-01-eq-1'))

    def test_expired_session_deactivates(self):
        with self.connect() as c:
            c.execute("DELETE FROM sessions")  # sesi habis/tak ada → gate mati
        self.insert_assignment()
        self.assertFalse(self.gate().is_active('ex-01-eq-1'))

    def test_ended_assignment_deactivates(self):
        self.insert_assignment(status='ended', ended=self.now_ms)
        self.assertFalse(self.gate().is_active('ex-01-eq-1'))

    def test_cache_still_used_by_is_active(self):
        from Sources.equipment.operation import OperationStatus
        status = OperationStatus(self.connect)
        status.set_cached('cached-eq-1', True, int(self.now_ms) + 60_000)
        self.assertTrue(status.is_active('cached-eq-1'))
        status.set_cached('cached-eq-1', False)
        self.assertFalse(status.is_active('cached-eq-1'))

    # ---- boot hygiene ---------------------------------------------------------
    def test_resolve_stale_actives_closes_and_counts(self):
        import time
        from Sources.alerts.store import resolve_stale_actives
        self.register_equipment('ex-01-eq-1')  # FK alerts→equipment (skej tim 9724576)
        now_ms = int(time.time() * 1000)
        with self.connect() as c:
            camera_id_row = c.execute(
                "INSERT INTO cameras(name, location, type, source, playback_fps, "
                "loop, registered_at_ms) VALUES ('Test Cam', NULL, 'recording', "
                "'/tmp/x.mp4', 8, true, %s) RETURNING id::text",
                (now_ms,)).fetchone()
            camera_id = camera_id_row[0]
        with self.connect() as c:
            for kind, status in (('person_entered_zone', 'active'),
                                 ('person_high_risk', 'active'),
                                 ('person_count_changed', 'active'),
                                 ('person_entered_zone', 'resolved')):
                c.execute("INSERT INTO alerts(kind, message, camera_id, "
                          "equipment_instance_id, persons_in_zone, occurred_at_ms, "
                          "created_at_ms, status, resolved_at_ms) "
                          "VALUES (%s, 'm', %s::uuid, 'ex-01-eq-1', 1, %s, %s, %s, %s)",
                          (kind, camera_id, now_ms, now_ms, status,
                           now_ms if status == 'resolved' else None))
        closed = resolve_stale_actives(connection_factory=self.connect)
        self.assertEqual(closed, 2)  # person_entered + person_high; count_changed & resolved tak tersentuh
        with self.connect() as c:
            rows = c.execute("SELECT kind, status FROM alerts ORDER BY kind").fetchall()
        self.assertEqual([r[1] for r in rows if r[0].startswith('person_entered')][0], 'resolved')
        self.assertEqual(resolve_stale_actives(connection_factory=self.connect), 0)  # idempoten


if __name__ == '__main__':
    unittest.main()
