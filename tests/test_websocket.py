"""Test kanal live WebSocket (RFC 6455) — api-draft 4.8 (S6/M7b).

Pure unit — codec frame + handshake — tanpa database (RUN_DATABASE_TESTS tidak
perlu). Import dari Sources.api.websocket (adapter stdlib; tidak menyentuh
modul fitur). Vektor handshake memakai contoh resmi RFC 6455 §1.3.
"""

import struct
import sys
import unittest
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Sources.alerts.bus import EventBus
from Sources.api.handlers import Handler
from Sources.api.websocket import (Opcode, accept_key, decode_frame,
                                   encode_close, encode_frame, encode_text,
                                   handle_websocket, is_upgrade_request,
                                   upgrade_response)


def _masked_frame(payload: bytes, key=b'\x01\x02\x03\x04',
                  opcode=Opcode.TEXT) -> bytes:
    """Bangun frame client (masked) untuk uji decode."""
    frame = bytes([0x80 | opcode, 0x80 | len(payload)])
    frame += key
    frame += bytes(payload[i] ^ key[i % 4] for i in range(len(payload)))
    return frame


class WebSocketHandshakeTests(unittest.TestCase):
    def test_accept_key_known_vector(self):
        # Vektor resmi RFC 6455 §1.3.
        self.assertEqual(accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
                         "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_is_upgrade_request_with_headers(self):
        self.assertTrue(is_upgrade_request({
            'Upgrade': 'websocket',
            'Connection': 'Upgrade',
            'Sec-WebSocket-Key': 'x',
        }))

    def test_is_upgrade_request_without_upgrade(self):
        self.assertFalse(is_upgrade_request({'Accept': 'text/plain'}))
        self.assertFalse(is_upgrade_request({'Upgrade': 'h2c'}))            # bukan websocket
        self.assertFalse(is_upgrade_request({'Sec-WebSocket-Key': 'x'}))   # tanpa Upgrade

    def test_upgrade_response_format(self):
        text = upgrade_response("dGhlIHNhbXBsZSBub25jZQ==").decode()
        self.assertTrue(text.startswith("HTTP/1.1 101 Switching Protocols\r\n"))
        self.assertIn("Upgrade: websocket\r\n", text)
        self.assertIn("Connection: Upgrade\r\n", text)
        self.assertIn("Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n", text)
        self.assertTrue(text.endswith("\r\n"))


class WebSocketFrameTests(unittest.TestCase):
    def test_round_trip_small_text(self):
        payload = b"hello"
        frame = encode_frame(payload)
        opcode, decoded, consumed = decode_frame(frame)
        self.assertEqual(opcode, Opcode.TEXT)
        self.assertEqual(decoded, payload)
        self.assertEqual(consumed, len(frame))

    def test_round_trip_medium_text(self):
        payload = b"x" * 200  # 126..65535 → extended 16-bit length
        frame = encode_frame(payload)
        self.assertEqual(frame[1] & 0x7F, 126)  # marker panjang extended
        opcode, decoded, consumed = decode_frame(frame)
        self.assertEqual(opcode, Opcode.TEXT)
        self.assertEqual(decoded, payload)
        self.assertEqual(consumed, len(frame))

    def test_round_trip_empty_text(self):
        frame = encode_frame(b"")
        self.assertEqual(frame, b"\x81\x00")
        opcode, decoded, consumed = decode_frame(frame)
        self.assertEqual(opcode, Opcode.TEXT)
        self.assertEqual(decoded, b"")
        self.assertEqual(consumed, 2)

    def test_encode_text(self):
        frame = encode_text("caf\u00e9")
        _, decoded, _ = decode_frame(frame)
        self.assertEqual(decoded, "caf\u00e9".encode())

    def test_decode_masked_client_frame(self):
        payload = b"ping-123"
        frame = _masked_frame(payload)
        opcode, decoded, consumed = decode_frame(frame)
        self.assertEqual(opcode, Opcode.TEXT)
        self.assertEqual(decoded, payload)
        self.assertEqual(consumed, len(frame))

    def test_decode_incomplete_frame(self):
        # Header bilang 10 byte payload, buffer cuma 2 → belum lengkap.
        opcode, payload, consumed = decode_frame(b"\x81\x0a")
        self.assertIsNone(opcode)
        self.assertIsNone(payload)
        self.assertEqual(consumed, 0)

    def test_decode_extended_length_incomplete(self):
        # Length 126 (extended 16-bit): butuh 4 byte, cuma 3.
        self.assertIsNone(decode_frame(b"\x81\x7e\x00")[0])

    def test_encode_close_with_reason(self):
        frame = encode_close(code=1000, reason='done')
        self.assertEqual(frame[0], 0x88)  # FIN + CLOSE
        opcode, payload, _ = decode_frame(frame)
        self.assertEqual(opcode, Opcode.CLOSE)
        self.assertEqual(payload, struct.pack('>H', 1000) + b'done')

    def test_encode_close_default(self):
        opcode, payload, _ = decode_frame(encode_close())
        self.assertEqual(opcode, Opcode.CLOSE)
        self.assertEqual(payload, struct.pack('>H', 1000))


class WebSocketIntegrationTests(unittest.TestCase):
    def test_http_handler_dispatches_authenticated_upgrade(self):
        auth = Mock()
        auth.authenticate.return_value = {
            'id': 'operator-1',
            'role': 'operator',
        }
        bus = object()
        handler = SimpleNamespace(
            server=SimpleNamespace(context=SimpleNamespace(auth=auth, bus=bus, connect=Mock())),
            path='/ws',
            headers={
                'Upgrade': 'websocket',
                'Connection': 'Upgrade',
                'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ==',
            },
            bearer=lambda: 'session-token',
            error=Mock(),
        )

        with patch('Sources.api.handlers.handle_websocket') as websocket_handler:
            Handler.do_GET(handler)

        auth.authenticate.assert_called_once_with('session-token')
        websocket_handler.assert_called_once()
        args, kwargs = websocket_handler.call_args
        self.assertEqual(args, (handler, bus, 'operator', 'operator-1'))
        self.assertTrue(callable(kwargs['reauthenticate']))
        handler.error.assert_not_called()

    def test_handler_pumps_event_and_unsubscribes_after_disconnect(self):
        event = b'event: alert.created\ndata: {"id":"alert-1"}\n\n'

        class ImmediateBus:
            def __init__(self):
                self.listener = None
                self.unsubscribed = None

            def subscribe(self, listener, role, operator_id=None):
                self.listener = listener
                self.role = role
                self.operator_id = operator_id
                listener.put(event)

            def unsubscribe(self, listener):
                self.unsubscribed = listener

        class DisconnectingWriter:
            def __init__(self):
                self.writes = []
                self.flushes = 0

            def write(self, data):
                self.writes.append(data)

            def flush(self):
                self.flushes += 1
                if self.flushes == 2:
                    raise BrokenPipeError()

        bus = ImmediateBus()
        writer = DisconnectingWriter()
        handler = SimpleNamespace(
            headers={'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ=='},
            wfile=writer,
        )

        handle_websocket(handler, bus, 'operator', 'operator-1')

        self.assertTrue(writer.writes[0].startswith(
            b'HTTP/1.1 101 Switching Protocols\r\n'))
        opcode, payload, consumed = decode_frame(writer.writes[1])
        self.assertEqual(opcode, Opcode.TEXT)
        self.assertEqual(payload, event)
        self.assertEqual(consumed, len(writer.writes[1]))
        self.assertEqual(bus.operator_id, 'operator-1')
        self.assertIs(bus.unsubscribed, bus.listener)

    def test_handler_unsubscribes_when_session_reauthentication_fails(self):
        event = b'event: alert.created\ndata: {}\n\n'

        class ImmediateBus:
            def subscribe(self, listener, role, operator_id=None):
                self.listener = listener
                listener.put(event)

            def unsubscribe(self, listener):
                self.unsubscribed = listener

        writer = Mock()
        handler = SimpleNamespace(
            headers={'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ=='},
            wfile=writer,
        )
        bus = ImmediateBus()

        handle_websocket(
            handler, bus, 'operator', 'operator-1',
            reauthenticate=Mock(side_effect=RuntimeError('expired')))

        self.assertEqual(writer.write.call_count, 1)  # handshake saja
        self.assertIs(bus.unsubscribed, bus.listener)

    def test_handler_closes_when_authenticated_role_changes(self):
        event = b'event: alert.created\ndata: {}\n\n'

        class ImmediateBus:
            def subscribe(self, listener, role, operator_id=None):
                self.listener = listener
                listener.put(event)

            def unsubscribe(self, listener):
                self.unsubscribed = listener

        writer = Mock()
        handler = SimpleNamespace(
            headers={'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ=='},
            wfile=writer,
        )
        bus = ImmediateBus()

        handle_websocket(
            handler, bus, 'hse', 'hse-1',
            reauthenticate=Mock(return_value={
                'role': 'operator', 'id': 'hse-1',
            }))

        self.assertEqual(writer.write.call_count, 1)
        self.assertIs(bus.unsubscribed, bus.listener)

    def test_bus_event_round_trips_through_ws_frame(self):
        # "Bentuk event identik": payload text frame == blok SSE bus byte-per-byte.
        bus = EventBus()
        queue = Queue()
        bus.subscribe(queue, role='hse')
        bus.publish({"message": "Danger", "id": "1"})
        sse_block = queue.get_nowait()

        frame = encode_frame(sse_block)
        opcode, payload, consumed = decode_frame(frame)
        self.assertEqual(opcode, Opcode.TEXT)
        self.assertEqual(payload, sse_block)
        self.assertEqual(consumed, len(frame))

    def test_bus_typed_event_round_trips_through_ws_frame(self):
        bus = EventBus()
        queue = Queue()
        bus.subscribe(queue, role='hse')
        bus.publish_typed("equipment.status", {"zoneStatus": "warning"})

        opcode, payload, _ = decode_frame(encode_frame(queue.get_nowait()))
        self.assertEqual(opcode, Opcode.TEXT)
        self.assertTrue(payload.startswith(b"event: equipment.status\n"))


if __name__ == "__main__":
    unittest.main()
