-- Device + binding shift (api-draft 4.6; self-service operator).
-- M7 (keputusan 8 Nov 2026): penugasan permanen = data lokal app (P8), server
-- hanya perlu tahu dua hal — ke mana push APNs dikirim (device_tokens) dan siapa
-- sedang binding-shift di unit mana (shift_bindings, hangus bersama sesi).
CREATE TABLE device_tokens (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    push_token text NOT NULL CHECK (length(push_token) BETWEEN 64 AND 200),
    model text NOT NULL DEFAULT '',
    os_version text NOT NULL DEFAULT '',
    app_version text NOT NULL DEFAULT '',
    registered_at_ms bigint NOT NULL CHECK (registered_at_ms > 0)
);
CREATE INDEX device_tokens_operator_idx ON device_tokens(operator_id);

CREATE TABLE shift_bindings (
    operator_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    equipment_instance_id text NOT NULL CHECK (length(equipment_instance_id) BETWEEN 1 AND 200),
    set_at_ms bigint NOT NULL CHECK (set_at_ms > 0),
    PRIMARY KEY (operator_id, equipment_instance_id)
);
CREATE INDEX shift_bindings_equipment_idx ON shift_bindings(equipment_instance_id);