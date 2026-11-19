import http.client
import json
import os
from pathlib import Path
import sys
from threading import Thread
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Sources.auth.passwords import hash_password, verify_password
from Sources.auth.limits import LoginLimits


class PasswordTests(unittest.TestCase):
    def test_password_hashes_are_salted_and_verified(self):
        password = 'temporary-test-password'
        first, second = hash_password(password), hash_password(password)
        self.assertNotEqual(first, second)
        self.assertNotIn(password, first)
        self.assertTrue(verify_password(password, first))
        self.assertFalse(verify_password('incorrect', first))
        self.assertFalse(verify_password(password, 'not-a-hash'))
        with self.assertRaises(ValueError):
            hash_password('short')

    def test_login_attempt_limit(self):
        limits = LoginLimits()
        self.assertTrue(all(limits.allow('local') for _ in range(10)))
        self.assertFalse(limits.allow('local'))
        self.assertTrue(limits.allow('another-client'))


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1', 'Requires local PostgreSQL')
class AuthHTTPTests(unittest.TestCase):
    def test_authentication_and_access_over_http(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        from Sources.infrastructure.database.connection import database_url, connect
        from Sources.infrastructure.database.migrate import migrate
        from Sources.auth.service import AuthService, token_hash
        from Sources.users.create import create_user
        from Sources.alerts.server import AlertServer

        admin = psycopg.connect('dbname=postgres host=/tmp', autocommit=True)
        self.addCleanup(admin.close)
        name = 'safety_auth_test_' + uuid4().hex
        admin.execute(sql.SQL('CREATE DATABASE {} OWNER safety_app').format(sql.Identifier(name)))
        self.addCleanup(lambda: admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name))))
        factory = lambda: connect(make_conninfo(database_url(), dbname=name))
        with factory() as connection:
            migrate(connection)
        password = 'temporary-test-password'
        create_user('operator-test', 'Operator test', 'operator', password, factory)
        create_user('hse-test', 'HSE test', 'hse', password, factory)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            create_user('OPERATOR-TEST', 'Duplicate', 'operator', password, factory)
        with self.assertRaises(ValueError):
            create_user('admin-test', 'Invalid', 'admin', password, factory)
        server = AlertServer(('127.0.0.1', 0), AuthService(factory))
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def stop():
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
        self.addCleanup(stop)

        def request(method, path, body=None, token=None, raw=False):
            client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            headers = {'Content-Type': 'application/json'}
            if token:
                headers['Authorization'] = 'Bearer ' + token
            client.request(method, path, body if raw else json.dumps(body) if body is not None else None, headers)
            response = client.getresponse()
            payload = json.loads(response.read())
            status = response.status
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            client.close()
            return status, payload

        self.assertEqual(request('GET', '/auth/me')[0], 401)
        self.assertEqual(request('GET', '/events')[0], 401)
        wrong = request('POST', '/auth/login', {'username': 'operator-test', 'password': 'incorrect'})
        unknown = request('POST', '/auth/login', {'username': 'missing', 'password': 'incorrect'})
        self.assertEqual(wrong, unknown)
        self.assertEqual(wrong[0], 401)
        self.assertEqual(request('POST', '/auth/login', '{bad', raw=True)[0], 400)
        self.assertEqual(request('POST', '/auth/login', {'username': 'operator-test', 'password': password, 'role': 'hse'})[0], 400)
        status, login = request('POST', '/auth/login', {'username': 'OPERATOR-TEST', 'password': password})
        self.assertEqual(status, 200)
        token = login['access_token']
        self.assertEqual(login['user']['role'], 'operator')
        self.assertNotIn('password_hash', login['user'])
        self.assertEqual(request('GET', '/auth/me', token=token)[1]['user'], login['user'])
        # M6c2 (api-draft 4.8): operator kini BOLEH membuka /events —
        # hanya akan menerima event equipment miliknya (assignment aktif).
        # Di sini cukup memastikan pembukaan stream operator diterima.
        operator_stream = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        self.addCleanup(operator_stream.close)
        operator_stream.request('GET', '/events', headers={'Authorization': 'Bearer ' + token})
        operator_response = operator_stream.getresponse()
        self.assertEqual(operator_response.status, 200)
        self.assertEqual(operator_response.readline(), b': connected\n')
        operator_stream.close()
        self.assertEqual(request('POST', '/auth/me', {'role': 'hse'}, token=token)[0], 404)
        with factory() as connection:
            stored = connection.execute('SELECT token_hash FROM sessions').fetchone()[0]
            self.assertEqual(stored, token_hash(token))
            self.assertNotEqual(stored, token)
        self.assertEqual(request('POST', '/auth/logout', token=token)[0], 200)
        self.assertEqual(request('GET', '/auth/me', token=token)[0], 401)
        self.assertEqual(request('POST', '/auth/logout', token=token)[0], 401)

        # SSE stops forwarding immediately after the next auth recheck on logout.
        status, login = request('POST', '/auth/login', {'username': 'hse-test', 'password': password})
        self.assertEqual(status, 200)
        hse_token = login['access_token']
        client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        self.addCleanup(client.close)
        client.request('GET', '/events', headers={'Authorization': 'Bearer ' + hse_token})
        stream = client.getresponse()
        self.assertEqual(stream.status, 200)
        self.assertEqual(stream.readline(), b': connected\n')
        self.assertEqual(stream.readline(), b'\n')
        server.publish({'message': 'Test event'})
        self.assertEqual(stream.readline(), b'event: mac-message\n')
        self.assertIn(b'Test event', stream.readline())
        self.assertEqual(stream.readline(), b'\n')
        self.assertEqual(request('POST', '/auth/logout', token=hse_token)[0], 200)
        server.publish({'message': 'Must not arrive'})
        self.assertEqual(stream.read(), b'')
        client.close()

        # Expiry and current account state are checked on every authenticated request.
        _, login = request('POST', '/auth/login', {'username': 'operator-test', 'password': password})
        token = login['access_token']
        with factory() as connection:
            connection.execute("UPDATE sessions SET created_at=now()-interval '2 hours', expires_at=now()-interval '1 hour'")
        self.assertEqual(request('GET', '/auth/me', token=token)[0], 401)
        _, login = request('POST', '/auth/login', {'username': 'operator-test', 'password': password})
        with factory() as connection:
            connection.execute("UPDATE users SET is_active=false WHERE username='operator-test'")
        self.assertEqual(request('GET', '/auth/me', token=login['access_token'])[0], 401)
        self.assertEqual(request('POST', '/auth/login', {'username': 'operator-test', 'password': password})[0], 401)
        self.assertEqual(request('POST', '/auth/login', {'username': 'hse-test', 'password': password})[0], 200)
        self.assertEqual(request('POST', '/auth/login', {'username': 'hse-test', 'password': password})[0], 429)


if __name__ == '__main__':
    unittest.main()
