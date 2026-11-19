"""The final unknown event must survive the ordinary equipment-status throttle."""
import json
import sys
import unittest
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Sources.alerts.bus import EventBus
from Sources.alerts.server import AlertServer
from Sources.core.evaluator import RiskEvaluator


class ObservationStatusTests(unittest.TestCase):
    def test_source_loss_is_published_even_just_after_online_status(self):
        # Exercise production publication without opening HTTP sockets or a DB.
        server = object.__new__(AlertServer)
        bus = EventBus()
        listener = Queue()
        bus.subscribe(listener, role='hse')
        risk = RiskEvaluator(operating_for=lambda _: True)
        server.context = SimpleNamespace(bus=bus, risk=risk, store=None)
        source = '12345678-test-camera'
        online = dict(equipmentInstanceId='12345678-eq-1', zoneStatus='clear',
                      monitoringStatus='online', operationActive=True,
                      lastInZone=0, lastSeenMs=10_000)
        server.publish_statuses([online], source, 10_000)
        listener.get_nowait()
        server.publish_statuses([online], source, 10_100)
        self.assertTrue(listener.empty(), 'Ordinary frame updates remain throttled')
        with patch('Sources.alerts.server.time.time_ns', return_value=10_200_000_000):
            server.observation_unavailable(source)
        payload = json.loads(listener.get_nowait().decode().split('data: ', 1)[1])
        self.assertEqual(payload['event'], 'equipment.status')
        self.assertEqual(payload['data']['monitoringStatus'], 'unknown')
        self.assertEqual(payload['data']['zoneStatus'], 'unknown')
