-- Riwayat alert + deliveries (api-draft 4.7; P6: endpoint riwayat menutup celah
-- SSE yang tidak diputar-ulang; CONTEXT "Arti status": resolved ditentukan evaluasi
-- risiko server, bukan tindakan pengguna).
CREATE TABLE alerts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (length(kind) BETWEEN 1 AND 100),
    message text NOT NULL CHECK (length(message) BETWEEN 1 AND 500),
    source_id text NOT NULL,
    equipment_instance_id text,
    persons_in_zone integer NOT NULL DEFAULT 0 CHECK (persons_in_zone >= 0),
    occurred_at_ms bigint NOT NULL CHECK (occurred_at_ms > 0),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'resolved')),
    resolved_at_ms bigint,
    created_at_ms bigint NOT NULL CHECK (created_at_ms > 0),
    CHECK (status = 'active' OR resolved_at_ms IS NOT NULL),
    CHECK (status = 'resolved' OR resolved_at_ms IS NULL)
);
CREATE INDEX alerts_occurred_idx ON alerts(occurred_at_ms DESC, id DESC);
CREATE INDEX alerts_equipment_active_idx
    ON alerts (equipment_instance_id) WHERE status = 'active';

CREATE TABLE alert_deliveries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id uuid NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    device_id uuid REFERENCES devices(id) ON DELETE SET NULL,
    operator_id uuid REFERENCES users(id) ON DELETE SET NULL,
    target text NOT NULL CHECK (target IN ('iphone', 'watch')),
    status text NOT NULL CHECK (status IN ('sent', 'failed')),
    at_ms bigint NOT NULL CHECK (at_ms > 0)
);
CREATE INDEX alert_deliveries_alert_idx ON alert_deliveries(alert_id);
