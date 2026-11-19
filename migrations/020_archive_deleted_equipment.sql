-- Keep catalogue/FK identities and evidence; only the live camera link is removed.
ALTER TABLE equipment_instances ADD COLUMN deleted_at_ms bigint;
ALTER TABLE alerts ADD COLUMN equipment_name text;
ALTER TABLE alerts ADD COLUMN resolution_reason text;

UPDATE alerts a SET equipment_name=COALESCE(l.display_name, c.name, e.name)
FROM equipment_instances e
LEFT JOIN equipment_labels l USING (equipment_instance_id)
LEFT JOIN equipment_cameras ec USING (equipment_instance_id)
LEFT JOIN cameras c ON c.id=ec.camera_id
WHERE a.equipment_instance_id=e.equipment_instance_id;

-- Rollback (after rolling back app/server): drop these three additive columns.
-- Removed links are intentionally not recreated: reconnect through Add Excavator.
