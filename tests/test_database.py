"""Opt-in PostgreSQL integration tests using disposable, uniquely named databases."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1', 'Set RUN_DATABASE_TESTS=1 with PostgreSQL running')
class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        from Sources.infrastructure.database.connection import database_url
        cls.admin = psycopg.connect('dbname=postgres host=/tmp connect_timeout=5', autocommit=True)
        cls.addClassCleanup(cls.admin.close)
        cls.name = 'safety_test_' + uuid4().hex
        cls.restore_name = cls.name + '_restore'
        cls.admin.execute(sql.SQL('CREATE DATABASE {} OWNER safety_app').format(sql.Identifier(cls.name)))
        cls.addClassCleanup(cls.drop_database, cls.name)
        cls.admin.execute(sql.SQL('CREATE DATABASE {} OWNER safety_app').format(sql.Identifier(cls.restore_name)))
        cls.addClassCleanup(cls.drop_database, cls.restore_name)
        cls.url = make_conninfo(database_url(), dbname=cls.name)
        cls.restore_url = make_conninfo(database_url(), dbname=cls.restore_name)

    @classmethod
    def drop_database(cls, name):
        from psycopg import sql
        cls.admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))

    def test_database_lifecycle(self):
        import psycopg
        from Sources.infrastructure.database.connection import connect
        from Sources.infrastructure.database.migrate import migrate
        from Sources.infrastructure.database.backup import backup, pg_environment, pg_tool
        with connect(self.url) as connection:
            # 21 file migrasi aktif di checkout (001-016; angka boleh dobel
            # leksekusi antar-tim — idempoten per nama file). Kunci keamanan:
            # jumlah file di-disk == yang diterapkan; migrasi ulang = 0.
            import pathlib
            expected = len(list(pathlib.Path(migrate.__globals__['MIGRATIONS'])
                                .glob('[0-9]*.sql')))
            self.assertEqual(migrate(connection), expected)
            user_id = connection.execute("INSERT INTO users(username, display_name, password_hash, role) VALUES ('operator-test', 'Test', 'test-hash-not-a-real-account', 'operator') RETURNING id").fetchone()[0]
            self.assertEqual(migrate(connection), 0)
            self.assertEqual(connection.execute('SELECT count(*) FROM users').fetchone()[0], 1)
            with self.assertRaises(psycopg.errors.UniqueViolation):
                with connection.transaction():
                    connection.execute("INSERT INTO users(username, display_name, password_hash, role) VALUES ('OPERATOR-TEST', 'Duplicate', 'test-hash', 'operator')")
            with self.assertRaises(psycopg.errors.CheckViolation):
                with connection.transaction():
                    connection.execute("INSERT INTO users(username, display_name, password_hash, role) VALUES ('bad-role', 'Invalid', 'test-hash', 'admin')")
            old_time = connection.execute('SELECT updated_at FROM users WHERE id=%s', (user_id,)).fetchone()[0]
            new_time = connection.execute("UPDATE users SET display_name='Updated' WHERE id=%s RETURNING updated_at", (user_id,)).fetchone()[0]
            self.assertGreater(new_time, old_time)
            privileges = connection.execute('SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone()
            self.assertEqual(privileges, (False, False, False))
        with tempfile.TemporaryDirectory() as temp:
            archive = backup(Path(temp) / 'database.dump', self.url)
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                backup(archive, self.url)
            result = subprocess.run([pg_tool('pg_restore'), '--exit-on-error', '--no-password', '--no-owner', '--dbname', self.restore_name, str(archive)], env=pg_environment(self.restore_url), capture_output=True)
            self.assertEqual(result.returncode, 0, 'Backup restore failed')
            with connect(self.restore_url) as connection:
                self.assertEqual(connection.execute('SELECT id, display_name FROM users').fetchone(), (user_id, 'Updated'))
                self.assertEqual(migrate(connection), 0)
        # Migration failure must roll back DDL and migration bookkeeping together.
        from Sources.infrastructure.database.migrate import MIGRATIONS
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            original = MIGRATIONS / '001_create_users.sql'
            for migration in MIGRATIONS.glob('[0-9]*.sql'):
                (directory / migration.name).write_text(migration.read_text())
            (directory / '003_bad.sql').write_text('CREATE TABLE should_rollback(id int); SELECT * FROM missing_table_for_test;')
            with connect(self.url) as connection:
                with self.assertRaises(psycopg.errors.UndefinedTable):
                    migrate(connection, directory)
                self.assertIsNone(connection.execute("SELECT to_regclass('should_rollback')").fetchone()[0])
                self.assertEqual(connection.execute('SELECT count(*) FROM schema_migrations').fetchone()[0], expected)
            (directory / original.name).write_text(original.read_text() + '\n-- changed')
            with connect(self.url) as connection:
                with self.assertRaises(ValueError):
                    migrate(connection, directory)
        from Sources.infrastructure.database.connection import database_url
        from psycopg.conninfo import make_conninfo
        for host in ('127.0.0.1', '::1', '/tmp'):
            with self.assertRaises(psycopg.OperationalError):
                with psycopg.connect(make_conninfo(database_url(), host=host, password='intentionally-wrong-password'), connect_timeout=3):
                    pass


if __name__ == '__main__':
    unittest.main()
