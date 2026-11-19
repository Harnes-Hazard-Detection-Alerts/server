-- Preserve an immutable reference to the physical push target even when APNs
-- returns 410 and its mutable device_tokens row is removed.
ALTER TABLE alert_deliveries
    ADD COLUMN push_device_ref text;

UPDATE alert_deliveries
SET push_device_ref = push_device_id::text
WHERE channel = 'push' AND push_device_id IS NOT NULL;

CREATE INDEX alert_deliveries_push_device_ref_idx
    ON alert_deliveries(push_device_ref)
    WHERE push_device_ref IS NOT NULL;
