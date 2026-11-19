-- Routing alert must be deterministic: one equipment can be bound to at most
-- one operator at a time. Existing duplicate rows intentionally make this
-- migration fail and roll back so an operator can resolve the conflict without
-- silently deleting shift data.
CREATE UNIQUE INDEX shift_bindings_equipment_unique_idx
    ON shift_bindings(equipment_instance_id);
