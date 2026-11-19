-- Perangkat operator (api-draft 4.6; CONTEXT "Device Mapping": iPhone = titik kirim,
-- Watch tercermin lewat metadatanya — Watch tidak pernah bicara langsung ke server).
CREATE TABLE devices (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    model text NOT NULL CHECK (length(btrim(model)) BETWEEN 1 AND 100),
    os_version text NOT NULL CHECK (length(btrim(os_version)) BETWEEN 1 AND 50),
    app_version text NOT NULL CHECK (length(btrim(app_version)) BETWEEN 1 AND 50),
    watch_present boolean NOT NULL DEFAULT false,
    watch_model text,
    watch_os_version text,
    status text NOT NULL DEFAULT 'offline' CHECK (status IN ('online', 'offline')),
    last_seen_ms bigint,
    registered_at_ms bigint NOT NULL CHECK (registered_at_ms > 0),
    CHECK (watch_present = false OR (watch_model IS NOT NULL AND watch_os_version IS NOT NULL)),
    CHECK (watch_present = true OR (watch_model IS NULL AND watch_os_version IS NULL))
);
CREATE INDEX devices_operator_idx ON devices(operator_id);
