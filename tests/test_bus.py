"""Test khusus EventBus — event typed (api-draft 4.8 §4.8).

Kanal typed adalah TAMBAHAN di samping `mac-message` legacy; keduanya berbagi
satu counter sequence global. Audiens identik dengan publish() lama.
"""
import json
import sys
import unittest
from pathlib import Path
from queue import Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Sources.alerts.bus import (ALERT_CREATED, ALERT_RESOLVED, CAMERA_STATUS,
                                 EQUIPMENT_STATUS, EventBus)


def _parse(raw_bytes: bytes) -> dict:
    """Pecah satu pesan SSE `event: X\ndata: {json}\n\n` -> ganti data JSON."""
    text = raw_bytes.decode()
    header, _, body = text.partition("data: ")
    return header, json.loads(body)


class TypedEventBusTests(unittest.TestCase):
    def test_publish_typed_emits_typed_sse_envelope(self):
        bus = EventBus()
        queue = Queue()
        bus.subscribe(queue, role='hse')
        payload = {"equipmentInstanceId": "cam01-eq-1", "zoneStatus": "warning",
                   "detectionCounts": {"person": 1, "excavator": 1},
                   "lastSeenMs": 1793933909000}
        bus.publish_typed(EQUIPMENT_STATUS, payload)

        header, envelope = _parse(queue.get_nowait())
        self.assertTrue(header.startswith("event: equipment.status\n"))
        self.assertEqual(envelope["event"], EQUIPMENT_STATUS)
        self.assertEqual(envelope["data"], payload)
        self.assertEqual(envelope["sequence"], 1)
        self.assertIsInstance(envelope["serverSentAtUnixMs"], int)

    def test_sequence_is_shared_across_mac_message_and_typed(self):
        bus = EventBus()
        queue = Queue()
        bus.subscribe(queue, role='hse')
        bus.publish({"message": "Danger", "id": "1"})                 # seq 1 (mac-message)
        bus.publish_typed(EQUIPMENT_STATUS, {"zoneStatus": "danger"})  # seq 2 (typed)
        bus.publish({"message": "Danger", "id": "3"})                 # seq 3 (mac-message)

        sequences = []
        for _ in range(3):
            _, envelope = _parse(queue.get_nowait())
            sequences.append(envelope["sequence"])
        self.assertEqual(sequences, [1, 2, 3])  # monotonic, tak terputus

    def test_publish_typed_respects_audience(self):
        bus = EventBus()
        hse = Queue()
        target_op = Queue()
        other_op = Queue()
        bus.subscribe(hse, role='hse')
        bus.subscribe(target_op, role='operator', operator_id='op-a')
        bus.subscribe(other_op, role='operator', operator_id='op-b')
        bus.publish_typed(EQUIPMENT_STATUS, {"zoneStatus": "warning"},
                          route_operator_id='op-a')

        self.assertFalse(hse.empty())        # hse selalu menerima
        self.assertFalse(target_op.empty())  # operator matching
        self.assertTrue(other_op.empty())    # operator lain tidak menerima

    def test_throttle_ok_with_same_key_and_interval(self):
        bus = EventBus()
        key = "equipment:cam01-eq-1"
        self.assertTrue(bus._throttle_ok(key, 1.0, now=100.0))   # first OK
        self.assertFalse(bus._throttle_ok(key, 1.0, now=100.5))  # terlalu cepat
        self.assertTrue(bus._throttle_ok(key, 1.0, now=101.0))   # lewat 1s -> OK

    def test_publish_typed_without_subscribers_does_not_raise(self):
        bus = EventBus()
        bus.publish_typed(EQUIPMENT_STATUS, {"zoneStatus": "clear"})
        bus.publish_typed(CAMERA_STATUS, {"cameraId": "c", "status": "online",
                                          "atMs": 1})
        bus.publish_typed(ALERT_CREATED, {"id": "a"})
        bus.publish_typed(ALERT_RESOLVED, {"id": "a", "status": "resolved"})


if __name__ == "__main__":
    unittest.main()