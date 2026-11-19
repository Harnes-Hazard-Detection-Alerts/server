-- Connect permanent assignments and runtime shift bindings to the equipment
-- display-name catalog. Backfill first so upgrades preserve all existing rows.
WITH referenced_equipment AS (
    SELECT equipment_instance_id FROM assignments
    UNION
    SELECT equipment_instance_id FROM shift_bindings
), missing_equipment AS (
    SELECT referenced_equipment.equipment_instance_id
    FROM referenced_equipment
    LEFT JOIN equipment_instances
        USING (equipment_instance_id)
    WHERE equipment_instances.equipment_instance_id IS NULL
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

ALTER TABLE assignments
    ADD CONSTRAINT assignments_equipment_instance_fk
    FOREIGN KEY (equipment_instance_id)
    REFERENCES equipment_instances(equipment_instance_id)
    ON DELETE RESTRICT;

ALTER TABLE shift_bindings
    ADD CONSTRAINT shift_bindings_equipment_instance_fk
    FOREIGN KEY (equipment_instance_id)
    REFERENCES equipment_instances(equipment_instance_id)
    ON DELETE CASCADE;
