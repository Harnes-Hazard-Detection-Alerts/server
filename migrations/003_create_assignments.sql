-- Assignment excavator<->operator (api-draft 4.5; CONTEXT "Equipment Assignment":
-- identitas equipment = equipmentInstanceId dari tracking, BUKAN koordinat bbox;
-- 1 excavator maksimal 1 assignment aktif; unassign = soft-state untuk jejak audit).
CREATE TABLE assignments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    equipment_instance_id text NOT NULL
        CHECK (length(btrim(equipment_instance_id)) BETWEEN 1 AND 200),
    operator_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended')),
    assigned_at_ms bigint NOT NULL CHECK (assigned_at_ms > 0),
    ended_at_ms bigint,
    ended_reason text,
    CHECK (status = 'active' OR (ended_at_ms IS NOT NULL AND ended_reason IS NOT NULL)),
    CHECK (status = 'ended' OR (ended_at_ms IS NULL AND ended_reason IS NULL))
);
CREATE UNIQUE INDEX assignments_one_active_per_equipment
    ON assignments (equipment_instance_id) WHERE status = 'active';
CREATE INDEX assignments_operator_active_idx
    ON assignments (operator_id) WHERE status = 'active';
