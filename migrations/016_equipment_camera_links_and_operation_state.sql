-- Explicit one-camera/one-equipment mapping for the MVP. Preserve the IDs that
-- were previously synthesized from the first eight camera UUID characters.
CREATE TABLE equipment_cameras (
    equipment_instance_id text PRIMARY KEY
        CHECK (length(equipment_instance_id) BETWEEN 1 AND 200),
    camera_id uuid NOT NULL UNIQUE REFERENCES cameras(id) ON DELETE RESTRICT,
    created_at_ms bigint NOT NULL,
    created_by uuid REFERENCES users(id) ON DELETE SET NULL
);

INSERT INTO equipment_cameras(equipment_instance_id, camera_id, created_at_ms)
SELECT left(id::text, 8) || '-eq-1', id,
       COALESCE(registered_at_ms, (extract(epoch FROM now()) * 1000)::bigint)
FROM cameras
;

-- A login binding now means "operator present". Operating starts only after an
-- explicit operator action. Preserve currently-running bindings on migration.
ALTER TABLE shift_bindings ADD COLUMN operation_started_at_ms bigint;
UPDATE shift_bindings SET operation_started_at_ms = set_at_ms;
