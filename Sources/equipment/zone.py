"""Radius zona bahaya per excavator (api-draft 4.4a).

HSE mengatur radius 2–10 m per `equipmentInstanceId` dari app. Nilai
disimpan di tabel `equipment_zone_settings` dan di-cache di memori supaya
evaluator (dipanggil tiap frame tiap kamera) tidak menembak database.
`radius_for` disuntik ke RiskEvaluator oleh composition root (main.py);
modul ini tidak diimpor core (aturan clean-lite).
"""
import threading

import psycopg

from ..infrastructure.database.connection import connect

MIN_RADIUS_M = 2.0
MAX_RADIUS_M = 10.0


class ZoneSettings:
    """Cache thread-safe radius per equipment, berbasis database."""

    def __init__(self, default_radius_m, connection_factory=connect):
        self.default_radius_m = float(default_radius_m)
        self._connect = connection_factory
        self._cache = {}
        self._lock = threading.Lock()

    def load(self):
        """Muat semua nilai tersimpan. Gagal database = fail-open (radius global)."""
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    'SELECT equipment_instance_id, zone_radius_m '
                    'FROM equipment_zone_settings').fetchall()
        except (psycopg.Error, ValueError):
            return False
        with self._lock:
            self._cache = {row[0]: float(row[1]) for row in rows}
        return True

    def radius_for(self, equipment_instance_id):
        """Radius unit (meter) atau None bila memakai radius global."""
        with self._lock:
            return self._cache.get(equipment_instance_id)

    def item(self, equipment_instance_id):
        custom = self.radius_for(equipment_instance_id)
        return {
            'equipmentInstanceId': equipment_instance_id,
            'zoneRadiusM': custom if custom is not None else self.default_radius_m,
            'defaultRadiusM': self.default_radius_m,
            'custom': custom is not None,
            'minRadiusM': MIN_RADIUS_M,
            'maxRadiusM': MAX_RADIUS_M,
        }

    def set(self, equipment_instance_id, radius_m, user_id, now_ms):
        """Simpan radius unit (upsert) lalu perbarui cache."""
        radius_m = float(radius_m)
        if not MIN_RADIUS_M <= radius_m <= MAX_RADIUS_M:
            raise ValueError('zone_radius_out_of_range')
        with self._connect() as connection:
            connection.execute(
                'INSERT INTO equipment_zone_settings(equipment_instance_id, '
                'zone_radius_m, updated_at_ms, updated_by) VALUES (%s, %s, %s, %s) '
                'ON CONFLICT (equipment_instance_id) DO UPDATE SET '
                'zone_radius_m=EXCLUDED.zone_radius_m, '
                'updated_at_ms=EXCLUDED.updated_at_ms, updated_by=EXCLUDED.updated_by',
                (equipment_instance_id, radius_m, now_ms, user_id))
        with self._lock:
            self._cache[equipment_instance_id] = radius_m
        return self.item(equipment_instance_id)
