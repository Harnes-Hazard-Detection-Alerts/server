"""Port milik domain — diimplementasi lapisan adapter (aturan clean-lite).

Port lahir bersama konsumen pertamanya saja (CONTEXT.md 7 Nov 2026):
- Detector (M4): dipakai AlertServer.process_frame; adapter utama
  Sources/detection/model.py (ultralytics), adapter uji = stub.
- Publisher (M5): bus publish/subscribe alert; adapter utama
  Sources/alerts/bus.py (antrean SSE), adapter uji = stub.
- AlertHistory (M6): penyimpanan riwayat alert; adapter utama
  Sources/alerts/store.py (PostgreSQL), adapter uji = stub.

Protocol structural: kecocokan diperiksa type checker; runtime tetap duck
typing — adapter tidak dipaksa mewarisi apa pun. Stdlib saja.
"""
from queue import Queue
from typing import Protocol

from .detection import Detection


class Detector(Protocol):
    """Sumber deteksi per frame; kesiapan dinyatakan lewat `ready`."""

    @property
    def ready(self) -> bool:
        """True bila detektor siap memproses frame (model termuat)."""
        ...

    def detect(self, frame) -> tuple[Detection, ...]:
        """Deteksi satu frame; tuple kosong saat gagal (fail-open)."""
        ...


class Publisher(Protocol):
    """Bus publikasi alert; satu listener = satu antrean SSE (audiens M6).

    hse menerima semua; operator HANYA event yang di-route kepadanya
    (assignment aktif untuk equipment di alert)."""

    def subscribe(self, listener: Queue, role: str = 'hse',
                  operator_id: str | None = None) -> None:
        """Daftarkan antrean dengan audiensnya (idempotent per queue)."""
        ...

    def unsubscribe(self, listener: Queue) -> None:
        """Cabut listener; tanpa error bila tak terdaftar."""
        ...

    def publish(self, alert: dict, route_operator_id: str | None = None) -> None:
        """Beri nomor urut & dorong ke listener yang berhak menerima."""
        ...

    def publish_typed(self, event_type: str, payload: dict,
                      route_operator_id: str | None = None) -> None:
        """Terbitkan event typed (kanal tambahan di samping mac-message).

        Envelope {event, data, sequence, serverSentAtUnixMs}; audiens sama
        dengan publish() (hse semua; operator hanya bila di-route)."""
        ...

    def online_operators(self) -> set:
        """operator_id yang punya stream hidup (presence SSE)."""
        ...


class AlertHistory(Protocol):
    """Riwayat alert (M6): hasil evaluasi risiko server — bukan konfirmasi user."""

    def record(self, alert: dict, now_ms: int) -> dict:
        """Simpan satu alert; zone_cleared menutup alert aktif yang sesuai."""
        ...

    def list(self, limit: int = 50, cursor: str | None = None,
             equipment_instance_id: str | None = None,
             operator_id: str | None = None, kind: str | None = None,
             status: str | None = None, from_ms: int | None = None,
             to_ms: int | None = None):
        """(items, nextCursor) — terbaru dulu, keyset cursor."""
        ...

    def get(self, alert_id: str):
        """Detail + deliveries; None bila tak dikenal."""
        ...
