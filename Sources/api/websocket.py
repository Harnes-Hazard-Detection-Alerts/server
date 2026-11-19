"""WebSocket kanal live (RFC 6455) — api-draft 4.8 (S6/M7b).

SSE /events dipertahankan selama transisi; /ws adalah kanal target.
Bentuk event identik — sama envelope SSE typed + mac-message: payload text
frame WS adalah byte yang persis sama dengan blok SSE (event: X\ndata: {...}).
Handshake: HTTP Upgrade; frame codec: text ONLY (binary tidak dipakai).

Adapter stdlib — tidak mengimpor modul fitur apa pun (guard arsitektur api/).
"""

import base64
import hashlib
import logging
import struct
import threading
import time
from enum import IntEnum
from queue import Empty, Queue

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def accept_key(client_key: str) -> str:
    """Compute Sec-WebSocket-Accept for handshake (RFC 6455 §4.2.2)."""
    digest = hashlib.sha1((client_key + WS_GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def is_upgrade_request(headers) -> bool:
    """Detect WebSocket upgrade in HTTP request headers."""
    return (headers.get('Upgrade', '').lower() == 'websocket'
            and 'Sec-WebSocket-Key' in headers)


def upgrade_response(client_key: str) -> bytes:
    """Build 101 Switching Protocols response."""
    accept = accept_key(client_key)
    return (f"HTTP/1.1 101 Switching Protocols\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            f"\r\n").encode()


# --- Frame encoder/decoder (text frames only) ---


class Opcode(IntEnum):
    CONTINUATION = 0x0
    TEXT = 0x1
    BINARY = 0x2
    CLOSE = 0x8
    PING = 0x9
    PONG = 0xA


def encode_frame(data: bytes, opcode=Opcode.TEXT) -> bytes:
    """Encode a WebSocket frame (server→client, no mask)."""
    header = bytes([0x80 | opcode])  # FIN=1, opcode
    length = len(data)
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + struct.pack('>H', length)
    else:
        header += bytes([127]) + struct.pack('>Q', length)
    return header + data


def encode_text(text: str, opcode=Opcode.TEXT) -> bytes:
    return encode_frame(text.encode(), opcode)


def encode_close(code=1000, reason='') -> bytes:
    payload = struct.pack('>H', code)
    if reason:
        payload += reason.encode()
    return encode_frame(payload, Opcode.CLOSE)


def decode_frame(buffer: bytes):
    """Decode first WebSocket frame from buffer.

    Returns (opcode, payload_bytes, bytes_consumed) or (None, None, 0) if
    incomplete. Handles: FIN, mask (client→server frames are MASKED), single
    frame only.
    """
    if len(buffer) < 2:
        return None, None, 0
    b1, b2 = buffer[0], buffer[1]
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F

    offset = 2
    if length == 126:
        if len(buffer) < 4:
            return None, None, 0
        length = struct.unpack('>H', buffer[2:4])[0]
        offset = 4
    elif length == 127:
        if len(buffer) < 10:
            return None, None, 0
        length = struct.unpack('>Q', buffer[2:10])[0]
        offset = 10

    mask_key = b''
    if masked:
        if len(buffer) < offset + 4:
            return None, None, 0
        mask_key = buffer[offset:offset + 4]
        offset += 4

    if len(buffer) < offset + length:
        return None, None, 0  # incomplete frame

    payload = buffer[offset:offset + length]
    if masked and mask_key:
        # Unmask using XOR cycling (RFC 6455 §5.3).
        payload = bytes(payload[i] ^ mask_key[i % 4]
                        for i in range(len(payload)))

    return opcode, payload, offset + length


# --- WebSocket Handler (used by API handler when /ws detected) ---


def handle_websocket(handler, bus, role, operator_id, logger=None,
                     reauthenticate=None, connection_factory=None):
    """Upgrade HTTP connection to WebSocket and pump events from bus queue.

    Dipanggil dari do_GET saat path='/ws' dan header Upgrade hadir:
    1. Kirim 101 Switching Protocols
    2. Subscribe bus (Queue) dengan audiens role/operator_id
    3. Loop: baca event bus → bungkus sebagai text frame WS → kirim
    4. Saat putus (ConnectionResetError/BrokenPipeError/OSError) → unsubscribe

    Operator terbitkan event `operator.presence` saat socket terbuka/tertutup
    (online=true/false) — sumber realtime HSE "No connection" saat operasi aktif
    (api-draft §4.8). Equipment diambil dari shift_bindings operator itu.
    """
    logger = logger or logging.getLogger('appleapp.ws')

    client_key = handler.headers.get('Sec-WebSocket-Key', '')
    handler.wfile.write(upgrade_response(client_key))
    handler.wfile.flush()

    listener = Queue()
    bus.subscribe(listener, role=role,
                  operator_id=operator_id if role == 'operator' else None)
    logger.info("ws=%s opened role=%s", id(handler), role)
    # Snapshot equipment binding login operator ini, untuk dipakai
    # saat socket tutup — bila operator Finish Operating lalu socket putus,
    # binding sudah hilang dan query close tidak akan menemukan equipment.
    operator_equipment_ids = None
    if role == 'operator' and operator_id is not None:
        operator_equipment_ids = _publish_operator_presence(
            bus, operator_id, True, connection_factory)

    try:
        last_heartbeat = time.monotonic()

        def verify_identity():
            if reauthenticate is None:
                return
            current = reauthenticate()
            if (current.get('role') != role
                    or current.get('id') != operator_id):
                raise PermissionError('websocket identity changed')

        while True:
            try:
                event = listener.get(timeout=10)
            except Empty:
                verify_identity()
                # Heartbeat text tidak meminta Pong. Adapter server ini hanya
                # memompa event keluar dan sengaja tidak menyimpan frame masuk.
                if time.monotonic() - last_heartbeat > 10:
                    handler.wfile.write(encode_frame(b': heartbeat\n\n'))
                    handler.wfile.flush()
                    last_heartbeat = time.monotonic()
                continue
            verify_identity()
            # Blok SSE → payload text frame WS (byte identik; transport beda).
            handler.wfile.write(encode_frame(event))
            handler.wfile.flush()
            last_heartbeat = time.monotonic()
    except Exception as error:
        # Unauthorized dan kegagalan socket sama-sama menutup stream. Error lain
        # dicatat agar kegagalan internal tidak tersamarkan sebagai disconnect.
        if not isinstance(error, (ConnectionResetError, BrokenPipeError, OSError)):
            logger.info("ws=%s authorization ended: %s", id(handler),
                        type(error).__name__)
        pass
    finally:
        bus.unsubscribe(listener)
        if role == 'operator' and operator_id is not None:
            _publish_operator_presence(bus, operator_id, False, connection_factory,
                                       equipment_ids=operator_equipment_ids)


def _publish_operator_presence(bus, operator_id, online, connection_factory,
                                equipment_ids=None):
    """Terbitkan operator.presence ke HSE untuk tiap equipment yang sedang
    terikat ke operator itu (termasuk Ready sebelum Start). Dipanggil saat socket
    terbuka/tertutup. Mengembalikan list equipment yang diterbitkan (untuk
    dipakai saat close).

    `online` di sini adalah hasil query `online_operators()` SETELAH
    subscribe/unsubscribe bus — sehingga status yang diterbitkan adalah
    kebenaran bus saat ini, bukan asumsi per-socket. Ini mencegah
    false-negative: bila operator punya socket lain masih hidup, satu
    socket yang tutup tidak akan terbit `online: false`.

    `equipment_ids` (opsional) dipakai saat socket tutup untuk menerbitkan
    ke equipment yang sebelumnya tercatat saat socket buka — sehingga
    bila operator Finish Operating (binding hilang) lalu socket putus,
    event `online: false` tetap terbit ke equipment yang benar.
    """
    if not hasattr(bus, 'publish_typed'):
        return equipment_ids or []
    # Sumber kebenaran: bus tahu operator mana yang masih punya subscriber hidup.
    actual_online = (operator_id in bus.online_operators()) if online is False else True
    # Pada socket close, gunakan equipment_ids yang dikumpulkan saat buka
    # (binding bisa sudah hilang saat close — Finish Operating dulu, baru
    # socket putus). Pada socket open, query binding segar.
    # Include login bindings before Start: the same socket survives Start/Finish.
    # Filtering to operating bindings here loses the unit on a later disconnect.
    if equipment_ids is None:
        equipment_ids = []
        if connection_factory is not None:
            try:
                with connection_factory() as connection:
                    rows = connection.execute(
                        'SELECT b.equipment_instance_id FROM shift_bindings b '
                        'JOIN assignments a '
                        '  ON a.equipment_instance_id=b.equipment_instance_id '
                        '  AND a.operator_id=b.operator_id AND a.status=%s '
                        'JOIN sessions s ON s.user_id=b.operator_id '
                        '  AND s.expires_at>now() '
                        'WHERE b.operator_id=%s',
                        ('active', operator_id)).fetchall()
                equipment_ids = [str(r[0]) for r in rows]
            except Exception as error:
                logging.getLogger('appleapp.ws').info(
                    'presence lookup gagal: %s', type(error).__name__)
    for equipment_id in equipment_ids:
        bus.publish_typed('operator.presence', {
            'operatorId': str(operator_id),
            'equipmentInstanceId': equipment_id,
            'online': actual_online,
        })
    return equipment_ids


# --- Kanal audio panggilan (Call Cabin) ---


def handle_call_audio(handler, calls, call, role, leg, logger=None):
    """Pompa frame audio biner antar dua kaki panggilan lewat WebSocket.

    Dipanggil setelah `calls.open_audio` sukses (kesalahan HTTP sudah lewat).
    Thread penulis mengirim frame dari antrean `leg` ke socket ini; thread
    handler membaca frame masuk (masked) dan merelay ke kaki lawan. Server
    tidak menyimpan atau menafsirkan isi frame.
    """
    logger = logger or logging.getLogger('appleapp.ws')
    handler.close_connection = True
    client_key = handler.headers.get('Sec-WebSocket-Key', '')
    write_lock = threading.Lock()

    def send(frame):
        with write_lock:
            handler.wfile.write(frame)
            handler.wfile.flush()

    def writer():
        try:
            while True:
                try:
                    frame = leg.queue.get(timeout=5)
                except Empty:
                    if not calls.is_active(call):
                        break
                    continue
                if frame is None:
                    break
                send(encode_frame(frame, Opcode.BINARY))
            send(encode_close(1000))
        except OSError:
            pass

    try:
        handler.wfile.write(upgrade_response(client_key))
        handler.wfile.flush()
        logger.info("call=%s audio opened role=%s", call.id, role)
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        buffer = b''
        while True:
            try:
                chunk = handler.rfile.read1(65536)
            except TimeoutError:
                if not calls.is_active(call):
                    break
                continue
            if not chunk:
                break
            buffer += chunk
            while True:
                opcode, payload, used = decode_frame(buffer)
                if opcode is None:
                    break
                buffer = buffer[used:]
                if opcode == Opcode.BINARY:
                    calls.relay(call, role, payload)
                elif opcode == Opcode.PING:
                    send(encode_frame(payload, Opcode.PONG))
                elif opcode == Opcode.CLOSE:
                    return
            if len(buffer) > 70000:
                break  # frame tak wajar: putus
    except OSError:
        pass
    finally:
        calls.close_audio(call, role, leg)
        leg.close()
        logger.info("call=%s audio closed role=%s", call.id, role)
