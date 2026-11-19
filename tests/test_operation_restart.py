"""A continuing camera hazard requires a fresh workflow after Stop/Start."""
import os
import sys
import unittest
from pathlib import Path
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Sources.core.evaluator import RiskEvaluator
from test_risk import EXCAVATOR, NEAR_PERSON


class RestartTimerTests(unittest.TestCase):
    def test_restart_between_frames_resets_ten_second_window(self):
        session = [10_000]
        risk = RiskEvaluator(operation_started_for=lambda _: session[0], operating_for=lambda _: True)
        def feed(t):
            return risk.evaluate([EXCAVATOR, NEAR_PERSON], source_id='cam', now_ms=t)
        events = [feed(t) for t in range(10_000, 20_701, 300)]
        self.assertEqual(sum(e is not None and e['kind'] == 'hazard_active_10s' for e in events), 1)
        session[0] = 21_000
        events = [feed(t) for t in range(21_000, 31_000, 250)]
        self.assertFalse(any(e and e['kind'] == 'hazard_active_10s' for e in events))
        self.assertEqual(feed(31_000)['kind'], 'hazard_active_10s')
        self.assertIsNone(feed(31_250))

    def test_process_restart_preserves_current_session_milestone(self):
        risk = RiskEvaluator(operation_started_for=lambda _: 20_000, operating_for=lambda _: True,
                             active_hazard_for=lambda _: (10_000, 30_000))
        events = [risk.evaluate([EXCAVATOR, NEAR_PERSON], source_id='cam', now_ms=t)
                  for t in range(32_000, 43_000, 300)]
        self.assertFalse(any(e and e['kind'] == 'hazard_active_10s' for e in events))


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == '1', 'Requires local PostgreSQL')
class RestartDatabaseTests(unittest.TestCase):
    def test_confirm_finish_restart_requires_new_response_and_confirmation(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        from Sources.infrastructure.database.connection import connect, database_url
        from Sources.infrastructure.database.migrate import migrate
        from Sources.users.create import create_user
        from Sources.equipment.operation import set_operation_active, OperationError
        from Sources.alerts.store import record, record_operator_response, hse_hazard_action, OperatorResponseError
        name = 'safety_restart_test_' + uuid4().hex
        admin = psycopg.connect('dbname=postgres host=/tmp', autocommit=True)
        self.addCleanup(admin.close)
        admin.execute(sql.SQL('CREATE DATABASE {} OWNER safety_app').format(sql.Identifier(name)))
        self.addCleanup(lambda: admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name))))
        def factory():
            return connect(make_conninfo(database_url(), dbname=name))
        with factory() as c:
            migrate(c)
        op = create_user('restart-op', 'Test Operator', 'operator', 'temporary-test-password', factory)
        hse = create_user('restart-hse', 'Test HSE', 'hse', 'temporary-test-password', factory)
        second_hse = create_user('restart-hse2', 'Second HSE', 'hse', 'temporary-test-password', factory)
        camera = str(uuid4())
        unit = camera[:8] + '-eq-1'
        with factory() as c:
            c.execute('INSERT INTO cameras(id,name,source,registered_at_ms) VALUES (%s,%s,%s,%s)',(camera,'Test camera','/tmp/restart-test.mp4',1000))
            c.execute('INSERT INTO equipment_instances VALUES (%s,%s)',(unit,'Test unit'))
            c.execute('INSERT INTO assignments(equipment_instance_id,operator_id,assigned_at_ms) VALUES (%s,%s,%s)',(unit,op,1000))
            c.execute('INSERT INTO shift_bindings(operator_id,equipment_instance_id,set_at_ms) VALUES (%s,%s,%s)',(op,unit,1000))
            c.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES (%s,%s,now()+interval '1 day')",('a'*64,op))
        start = lambda t: set_operation_active(op,unit,True,t,factory)
        finish = lambda t: set_operation_active(op,unit,False,t,factory)
        def event(kind,t):
            return record(dict(kind=kind,message=kind,cameraId=camera,equipmentInstanceId=unit,personsInZone=1,frameTimestampMs=t),t,factory)
        # Login opens its socket while Ready, before the first Start. That
        # socket must still publish offline for this unit after Start/Finish.
        import json
        from queue import Queue
        from Sources.alerts.bus import EventBus
        from Sources.api.websocket import _publish_operator_presence
        bus = EventBus()
        hse_listener, operator_listener = Queue(), Queue()
        bus.subscribe(hse_listener, role='hse')
        bus.subscribe(operator_listener, role='operator', operator_id=op)
        equipment_ids = _publish_operator_presence(bus, op, True, factory)
        self.assertEqual(equipment_ids, [unit])
        online = json.loads(hse_listener.get_nowait().decode().split('data: ', 1)[1])
        self.assertTrue(online['data']['online'])
        start(10_000)
        bus.unsubscribe(operator_listener)
        _publish_operator_presence(bus, op, False, factory, equipment_ids=equipment_ids)
        offline = json.loads(hse_listener.get_nowait().decode().split('data: ', 1)[1])
        self.assertEqual(offline['data']['equipmentInstanceId'], unit)
        self.assertFalse(offline['data']['online'])
        # A second surviving socket must keep the operator online.
        bus.subscribe(operator_listener, role='operator', operator_id=op)
        _publish_operator_presence(bus, op, False, factory, equipment_ids=equipment_ids)
        still_online = json.loads(hse_listener.get_nowait().decode().split('data: ', 1)[1])
        self.assertTrue(still_online['data']['online'])
        alert = event('person_entered_zone',10_300)
        event('hazard_active_10s',20_300)
        alert = event('person_high_risk',20_500)
        self.assertIsNone(alert['operatorResponse'])
        self.assertEqual(alert['hazardActive10sAtMs'],20_300,
                         'Escalation must preserve the unanswered response prompt')
        record_operator_response(unit,op,'needs_hse_help',21_000,factory)
        hse_hazard_action(alert['id'],hse,'Test HSE','on_my_way',22_000,factory)
        hse_hazard_action(alert['id'],hse,'Test HSE','confirmed_safe',23_000,factory)
        finish(24_000)
        start(25_000)
        with factory() as c:
            row=c.execute('SELECT status,operator_response,hazard_active_10s_at_ms,hse_on_my_way_at_ms,hse_confirmed_safe_at_ms FROM alerts WHERE id=%s',(alert['id'],)).fetchone()
            self.assertEqual(row,('active',None,None,None,None))
            old=c.execute('SELECT snapshot FROM alert_workflow_history WHERE alert_id=%s',(alert['id'],)).fetchone()[0]
            self.assertEqual(old['operator_response'],'needs_hse_help')
            self.assertEqual(old['hse_confirmed_safe_at_ms'],23_000)
        with self.assertRaisesRegex(OperatorResponseError,'operator_response_required'):
            hse_hazard_action(alert['id'],hse,'Test HSE','confirmed_safe',26_000,factory)
        with self.assertRaisesRegex(OperationError,'active_hazard'):
            finish(26_000)
        self.assertIsNone(event('hazard_active_10s',26_000))
        self.assertIsNotNone(event('hazard_active_10s',35_000))
        record_operator_response(unit,op,'needs_hse_help',36_000,factory)
        start(36_500)
        hse_hazard_action(alert['id'],second_hse,'Second HSE','on_my_way',37_000,factory)
        hse_hazard_action(alert['id'],second_hse,'Second HSE','confirmed_safe',38_000,factory)
        finish(39_000)
        with factory() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM alert_workflow_history').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT status FROM alerts WHERE id=%s',(alert['id'],)).fetchone()[0],'active')
        event('zone_cleared',40_000)
        with factory() as c:
            self.assertEqual(c.execute('SELECT status FROM alerts WHERE id=%s',(alert['id'],)).fetchone()[0],'resolved')


if __name__ == '__main__':
    unittest.main()
