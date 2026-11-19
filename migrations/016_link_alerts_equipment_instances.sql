-- Alert history belongs to a stable equipment instance. Preserve legacy rows
-- by cataloguing any referenced id that predates the equipment name table,
-- then enforce the relationship without deleting history.
WITH missing_equipment AS (
    SELECT DISTINCT a.equipment_instance_id
    FROM alerts a
    LEFT JOIN equipment_instances e
        USING (equipment_instance_id)
    WHERE a.equipment_instance_id IS NOT NULL
      AND e.equipment_instance_id IS NULL
), numbered_equipment AS (
    SELECT
        equipment_instance_id,
        'Excavator - ' || (
            (SELECT count(*) FROM equipment_instances)
            + row_number() OVER (ORDER BY equipment_instance_id)
        )::text AS name
    FROM missing_equipment
)
INSERT INTO equipment_instances(equipment_instance_id, name)
SELECT equipment_instance_id, name
FROM numbered_equipment;

ALTER TABLE alerts
    ADD CONSTRAINT alerts_equipment_instance_fk
    FOREIGN KEY (equipment_instance_id)
    REFERENCES equipment_instances(equipment_instance_id)
    ON DELETE RESTRICT;
