"""Call Cabin (VoIP satu jaringan): siklus sinyal + relay audio.

Unit murni (tanpa database): CallService memakai bus nyata dan resolver
operator palsu. Satu uji end-to-end memakai Handler + socket sungguhan untuk
membuktikan relay audio dua arah lewat WebSocket.
"""
import json
import socket
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Sources.alerts.bus import EventBus
from Sources.api.handlers import Handler
from Sources.api.websocket import Opcode, decode_frame, encode_frame
from Sources.calls.service import CallError, CallService

HSE = {'id': 'hse-1', 'role': 'hse', 'username': 'hse', 'display_name': 'Pak HSE'}
OPERATOR = {'id': 'op-1', 'role': 'operator', 'username': 'op', 'display_name': 'Budi'}
OTHER = {'id': 'op-2', 'role': 'operator', 'username': 'x', 'display_name': 'X'}
UNIT = 'cam1-eq-1'


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_service(bound=OPERATOR['id'], online=True, clock=None):
    bus = EventBus()
    if online:
        bus.subscribe(Queue(), role='operator', operator_id=OPERATOR['id'])
    events = Queue()
    bus.subscribe(events, role='hse')
    service = CallService(bus, lambda equipment: bound if equipment == UNIT else None,
                          clock=clock or FakeClock())
    return service, events


def drain(events):
    items = []
    while not events.empty():
        block = events.get_nowait().decode()
        items.append(json.loads(block.split('data: ', 1)[1])['data'])
    return items


class CallSignalingTests(unittest.TestCase):
    def test_start_rings_bound_operator_and_publishes_state(self):
        service, events = make_service()
        call = service.start(HSE, UNIT)
        self.assertEqual((call['state'], call['operatorId'], call['hseName']),
                         ('ringing', OPERATOR['id'], 'Pak HSE'))
        self.assertEqual(drain(events)[0]['id'], call['id'])

    def test_start_rejects_unit_without_operator_or_offline_operator(self):
        service, _ = make_service(bound=None)
        with self.assertRaises(CallError) as caught:
            service.start(HSE, UNIT)
        self.assertEqual(caught.exception.code, 'no_operator')
        service, _ = make_service(online=False)
        with self.assertRaises(CallError) as caught:
            service.start(HSE, UNIT)
        self.assertEqual(caught.exception.code, 'operator_offline')

    def test_second_call_for_same_unit_is_rejected_until_first_ends(self):
        service, _ = make_service()
        first = service.start(HSE, UNIT)
        with self.assertRaises(CallError) as caught:
            service.start(HSE, UNIT)
        self.assertEqual(caught.exception.code, 'call_in_progress')
        service.end(first['id'], HSE)
        self.assertEqual(service.start(HSE, UNIT)['state'], 'ringing')

    def test_answer_then_end(self):
        service, _ = make_service()
        call = service.start(HSE, UNIT)
        self.assertEqual(service.answer(call['id'], OPERATOR)['state'], 'active')
        ended = service.end(call['id'], OPERATOR)
        self.assertEqual((ended['state'], ended['endReason']), ('ended', 'ended'))
        self.assertEqual(service.end(call['id'], HSE)['endReason'], 'ended')  # idempoten

    def test_decline_and_cancel_reasons(self):
        service, _ = make_service()
        declined = service.decline(service.start(HSE, UNIT)['id'], OPERATOR)
        self.assertEqual(declined['endReason'], 'declined')
        cancelled = service.end(service.start(HSE, UNIT)['id'], HSE)
        self.assertEqual(cancelled['endReason'], 'cancelled')

    def test_only_operator_answers_and_only_participants_see_call(self):
        service, _ = make_service()
        call = service.start(HSE, UNIT)
        with self.assertRaises(CallError) as caught:
            service.answer(call['id'], HSE)
        self.assertEqual(caught.exception.code, 'forbidden')
        with self.assertRaises(CallError) as caught:
            service.get(call['id'], OTHER)
        self.assertEqual(caught.exception.code, 'forbidden')
        with self.assertRaises(CallError) as caught:
            service.get('00000000-0000-0000-0000-000000000000', HSE)
        self.assertEqual(caught.exception.code, 'not_found')

    def test_answer_twice_is_invalid_state(self):
        service, _ = make_service()
        call = service.start(HSE, UNIT)
        service.answer(call['id'], OPERATOR)
        with self.assertRaises(CallError) as caught:
            service.answer(call['id'], OPERATOR)
        self.assertEqual(caught.exception.code, 'invalid_state')

    def test_unanswered_call_becomes_missed(self):
        clock = FakeClock()
        service, events = make_service(clock=clock)
        call = service.start(HSE, UNIT)
        clock.now += 31
        self.assertEqual(service.get(call['id'], HSE)['endReason'], 'missed')
        self.assertEqual(drain(events)[-1]['state'], 'ended')

    def test_active_call_without_audio_legs_becomes_lost(self):
        clock = FakeClock()
        service, _ = make_service(clock=clock)
        call = service.start(HSE, UNIT)
        service.answer(call['id'], OPERATOR)
        clock.now += 21
        self.assertEqual(service.get(call['id'], HSE)['endReason'], 'lost')


class CallAudioTests(unittest.TestCase):
    def test_audio_needs_active_call(self):
        service, _ = make_service()
        call = service.start(HSE, UNIT)
        with self.assertRaises(CallError) as caught:
            service.open_audio(call['id'], OPERATOR)
        self.assertEqual(caught.exception.code, 'invalid_state')

    def test_relay_goes_to_peer_only_and_drops_without_peer(self):
        service, _ = make_service()
        info = service.start(HSE, UNIT)
        service.answer(info['id'], OPERATOR)
        call, hse_role, hse_leg = service.open_audio(info['id'], HSE)
        self.assertFalse(service.relay(call, hse_role, b'x' * 640))  # lawan belum ada
        _, op_role, op_leg = service.open_audio(info['id'], OPERATOR)
        self.assertTrue(service.relay(call, hse_role, b'\x01' * 640))
        self.assertEqual(op_leg.queue.get_nowait(), b'\x01' * 640)
        self.assertTrue(hse_leg.queue.empty())
        self.assertFalse(service.relay(call, op_role, b'x' * 5000))  # terlalu besar

    def test_full_queue_drops_oldest_frame(self):
        service, _ = make_service()
        info = service.start(HSE, UNIT)
        service.answer(info['id'], OPERATOR)
        call, hse_role, _ = service.open_audio(info['id'], HSE)
        _, _, op_leg = service.open_audio(info['id'], OPERATOR)
        for number in range(60):
            service.relay(call, hse_role, bytes([number]) * 4)
        self.assertEqual(op_leg.queue.qsize(), 50)
        self.assertEqual(op_leg.queue.get_nowait(), bytes([10]) * 4)

    def test_ending_call_closes_legs(self):
        service, _ = make_service()
        info = service.start(HSE, UNIT)
        service.answer(info['id'], OPERATOR)
        _, _, leg = service.open_audio(info['id'], HSE)
        service.end(info['id'], HSE)
        self.assertIsNone(leg.queue.get_nowait())


# --- end-to-end: Handler + WebSocket sungguhan ------------------------------

TOKENS = {'h' * 43: HSE, 'o' * 43: OPERATOR}


class FakeAuth:
    def authenticate(self, token):
        from Sources.auth.service import Unauthorized
        if token not in TOKENS:
            raise Unauthorized()
        return TOKENS[token]


def masked(payload, opcode=Opcode.BINARY):
    key = b'\x0a\x0b\x0c\x0d'
    body = bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))
    if len(payload) < 126:
        head = bytes([0x80 | opcode, 0x80 | len(payload)])
    else:  # panjang 16-bit (frame audio 640 byte)
        head = bytes([0x80 | opcode, 0x80 | 126]) + len(payload).to_bytes(2, 'big')
    return head + key + body


def read_frame(sock):
    buffer = b''
    while True:
        opcode, payload, used = decode_frame(buffer)
        if opcode is not None:
            return opcode, payload
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError('socket closed')
        buffer += chunk


class CallEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.service, _ = make_service()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server.context = SimpleNamespace(
            auth=FakeAuth(), calls=self.service, bus=EventBus(), detector=None,
            login_limits=None)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, token, body=None):
        raw = json.dumps(body) if body is not None else ''
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as sock:
            sock.sendall((f'{method} {path} HTTP/1.1\r\nHost: x\r\n'
                          f'Authorization: Bearer {token}\r\n'
                          f'Content-Type: application/json\r\n'
                          f'Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n{raw}').encode())
            data = b''
            while chunk := sock.recv(4096):
                data += chunk
        head, _, payload = data.partition(b'\r\n\r\n')
        status = int(head.split()[1])
        return status, (json.loads(payload) if payload else None)

    def connect_audio(self, call_id, token):
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        sock.sendall((f'GET /calls/{call_id}/audio HTTP/1.1\r\nHost: x\r\n'
                      f'Authorization: Bearer {token}\r\nUpgrade: websocket\r\n'
                      'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                      'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n').encode())
        data = b''
        while b'\r\n\r\n' not in data:
            data += sock.recv(4096)
        self.assertIn(b'101', data.split(b'\r\n')[0])
        return sock

    def test_signaling_over_http(self):
        status, call = self.request('POST', '/calls', 'h' * 43, {'equipmentInstanceId': UNIT})
        self.assertEqual((status, call['state']), (201, 'ringing'))
        self.assertEqual(self.request('POST', '/calls', 'o' * 43,
                                      {'equipmentInstanceId': UNIT})[0], 403)
        self.assertEqual(self.request('POST', '/calls', 'h' * 43,
                                      {'equipmentInstanceId': UNIT})[0], 409)
        self.assertEqual(self.request('POST', f"/calls/{call['id']}/answer", 'h' * 43)[0], 403)
        status, answered = self.request('POST', f"/calls/{call['id']}/answer", 'o' * 43)
        self.assertEqual((status, answered['state']), (200, 'active'))
        self.assertEqual(self.request('GET', f"/calls/{call['id']}", 'h' * 43)[1]['state'], 'active')

    def test_audio_relays_both_directions(self):
        _, call = self.request('POST', '/calls', 'h' * 43, {'equipmentInstanceId': UNIT})
        self.request('POST', f"/calls/{call['id']}/answer", 'o' * 43)
        hse = self.connect_audio(call['id'], 'h' * 43)
        operator = self.connect_audio(call['id'], 'o' * 43)
        self.addCleanup(hse.close)
        self.addCleanup(operator.close)
        # Beri waktu kedua kaki terdaftar sebelum frame pertama.
        deadline = threading.Event()
        deadline.wait(0.3)
        hse.sendall(masked(b'\x11' * 640))
        self.assertEqual(read_frame(operator), (Opcode.BINARY, b'\x11' * 640))
        operator.sendall(masked(b'\x22' * 640))
        self.assertEqual(read_frame(hse), (Opcode.BINARY, b'\x22' * 640))
        self.request('POST', f"/calls/{call['id']}/end", 'h' * 43)
        self.assertEqual(read_frame(operator)[0], Opcode.CLOSE)


if __name__ == '__main__':
    unittest.main()
