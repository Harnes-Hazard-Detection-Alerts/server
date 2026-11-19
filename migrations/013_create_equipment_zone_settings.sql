-- Radius zona bahaya per excavator (keputusan owner 11 Nov 2026, slider
-- "Safety Zone" di app HSE). Tanpa baris = radius global server (4 m).
-- equipment_instance_id adalah ID string dari tracking, bukan UUID.
CREATE TABLE equipment_zone_settings (
    equipment_instance_id text PRIMARY KEY
        CHECK (length(equipment_instance_id) BETWEEN 1 AND 200),
    zone_radius_m double precision NOT NULL
        CHECK (zone_radius_m >= 2 AND zone_radius_m <= 10),
    updated_at_ms bigint NOT NULL,
    updated_by uuid REFERENCES users(id) ON DELETE SET NULL
);
