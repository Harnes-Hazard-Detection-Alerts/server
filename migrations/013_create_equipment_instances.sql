-- Display-name catalog for stable equipmentInstanceId values.
-- Runtime tracking state (boxes, confidence, zone status) deliberately remains
-- owned by the detection pipeline and is not persisted in this table.
CREATE TABLE equipment_instances (
    equipment_instance_id text PRIMARY KEY
        CHECK (
            equipment_instance_id = btrim(equipment_instance_id)
            AND length(equipment_instance_id) BETWEEN 1 AND 200
        ),
    name text NOT NULL
        CHECK (length(btrim(name)) BETWEEN 1 AND 100)
);

-- Preserve every equipment id already referenced by the database. Camera
-- fallback ids are included because the evaluator emits "{camera-prefix}-eq-1"
-- before a real multi-object tracker supplies its own stable ids.
WITH referenced_equipment AS (
    SELECT equipment_instance_id FROM assignments
    UNION
    SELECT equipment_instance_id FROM shift_bindings
    UNION
    SELECT equipment_instance_id FROM alerts
        WHERE equipment_instance_id IS NOT NULL
    UNION
    SELECT left(id::text, 8) || '-eq-1' FROM cameras
), numbered_equipment AS (
    SELECT
        equipment_instance_id,
        'Excavator - ' || (
            row_number() OVER (ORDER BY equipment_instance_id)
        )::text AS name
    FROM referenced_equipment
    WHERE length(btrim(equipment_instance_id)) BETWEEN 1 AND 200
)
INSERT INTO equipment_instances(equipment_instance_id, name)
SELECT equipment_instance_id, name
FROM numbered_equipment;
