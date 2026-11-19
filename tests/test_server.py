import io
import json
from pathlib import Path
from queue import Queue
import sys
import unittest
from unittest.mock import Mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Sources.alerts import server as backend
from Sources.alerts.bus import EventBus


class ServerContractTests(unittest.TestCase):
    def make_server(self):
        server = backend.AlertServer.__new__(backend.AlertServer)
        # Konteks aplikasi (M5): semua komponen lewat satu objek —
        # Handler menjangkau fitur hanya via self.server.context.*.
        # Detektor asli memuat model ML (berat, tergantung Server/.env);
        # test kontrak server cukup stub port Detector tanpa runtime.
        bus = EventBus()
        for _ in range(2):
            bus.subscribe(Queue())
        server.context = SimpleNamespace(
            auth=Mock(**{'authenticate.return_value': {'role': 'hse', 'id': 'hse-1'}}),
            login_limits=Mock(),
            risk=Mock(),
            detector=SimpleNamespace(ready=False, detect=Mock(return_value=())),
            bus=bus)
        server.test_bus_queues = [queue for queue, _, _ in bus.subscribers.values()]
        return server

    def test_publish_preserves_event_and_order_for_all_listeners(self):
        server = self.make_server()
        for sequence in (1, 2):
            alert = {"message": "Danger", "id": str(sequence)}
            server.publish(alert)
            for listener in server.test_bus_queues:
                event = listener.get_nowait().decode()
                self.assertTrue(event.startswith("event: mac-message\ndata: "))
                payload = json.loads(event.split("data: ", 1)[1])
                self.assertEqual(payload["alert"], alert)
                self.assertEqual(payload["message"], "Danger")
                self.assertEqual(payload["sequence"], sequence)
                self.assertIsInstance(payload["serverSentAtUnixMs"], int)

    def test_bus_publishes_typed_event_alongside_legacy(self):
        # Event typed (M7/4.8) adalah kanal tambahan di bus yang sama; legacy
        # mac-message tetap. Keduanya berbagi satu counter sequence global.
        server = self.make_server()
        bus = server.context.bus
        bus.publish({"message": "Danger", "id": "1"})            # seq 1 legacy
        payload = {"equipmentInstanceId": "cam01-eq-1", "zoneStatus": "clear",
                   "detectionCounts": {"person": 0, "excavator": 0},
                   "lastSeenMs": 1}
        bus.publish_typed("equipment.status", payload)           # seq 2 typed
        for listener in server.test_bus_queues:
            legacy = listener.get_nowait().decode()
            self.assertTrue(legacy.startswith("event: mac-message\ndata: "))
            typed = listener.get_nowait().decode()
            self.assertTrue(typed.startswith("event: equipment.status\ndata: "))
            envelope = json.loads(typed.split("data: ", 1)[1])
            self.assertEqual(envelope["event"], "equipment.status")
            self.assertEqual(envelope["data"], payload)
            self.assertEqual(envelope["sequence"], 2)

    def test_unloaded_detector_does_not_publish(self):
        server = self.make_server()
        server.publish = Mock()
        server.process_frame(object())
        server.publish.assert_not_called()

    def test_missing_detector_does_not_publish(self):
        server = self.make_server()
        server.context.detector = None
        server.publish = Mock()
        server.process_frame(object())
        server.publish.assert_not_called()

    def test_risk_event_is_published(self):
        server = self.make_server()
        server.context.detector = SimpleNamespace(ready=True, detect=Mock(return_value=({"classId": 2},)))
        server.context.risk = Mock()
        server.context.risk.evaluate.return_value = {"message": "Danger", "kind": "person_entered_zone"}
        server.context.risk.unit_statuses.return_value = []
        server.publish = Mock()
        server.process_frame("frame")
        server.context.detector.detect.assert_called_once_with("frame")
        server.context.risk.evaluate.assert_called_once_with(({"classId": 2},), timestamp_ms=None, source_id=None, now_ms=None)
        server.publish.assert_called_once_with({"message": "Danger", "kind": "person_entered_zone"}, route_operator_id=None)

    def test_stream_disconnect_is_logged_without_secrets_and_removes_listener(self):
        handler = backend.Handler.__new__(backend.Handler)
        handler.server = self.make_server()
        bus = handler.server.context.bus
        before = set(bus.subscribers)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = Mock()
        handler.wfile.write.side_effect = BrokenPipeError(32, 'private-diagnostic-secret')
        with self.assertLogs('appleapp.stream', level='INFO') as captured:
            handler.stream_events('private-token')
        output = '\n'.join(captured.output)
        self.assertIn('reason=BrokenPipeError errno=32', output)
        self.assertIn('opened', output)
        self.assertIn('removed', output)
        self.assertNotIn('private-token', output)
        self.assertNotIn('private-diagnostic-secret', output)
        self.assertEqual(set(bus.subscribers), before)

    def test_health_and_unknown_route(self):
        handler = backend.Handler.__new__(backend.Handler)
        handler.server = self.make_server()
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.send_error = Mock()
        handler.path = "/health"
        handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        payload = json.loads(handler.wfile.getvalue())
        self.assertEqual(payload["service"], "alert-server")
        self.assertFalse(payload["modelLoaded"])
        self.assertTrue(payload["ok"])
        handler.server.context.detector.ready = True
        handler.wfile = io.BytesIO()
        handler.do_GET()
        self.assertTrue(json.loads(handler.wfile.getvalue())["modelLoaded"])
        handler.path = "/missing"
        handler.do_GET()
        self.assertEqual(handler.send_response.call_args.args, (404,))


if __name__ == "__main__":
    unittest.main()
