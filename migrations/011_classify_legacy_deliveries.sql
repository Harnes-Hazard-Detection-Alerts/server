-- Rows written before migration 009 had no channel marker. A NULL device_id
-- could be an old push attempt, so calling every historical row "live" would
-- invent certainty. Preserve those ambiguous rows as legacy/unknown.
ALTER TABLE alert_deliveries DROP CONSTRAINT alert_deliveries_channel_check;
ALTER TABLE alert_deliveries ADD CONSTRAINT alert_deliveries_channel_check
    CHECK (channel IN ('live', 'push', 'legacy'));

UPDATE alert_deliveries
SET channel = 'legacy'
WHERE channel = 'live'
  AND device_id IS NULL
  AND push_device_id IS NULL
  AND at_ms < (
      SELECT (extract(epoch FROM applied_at) * 1000)::bigint
      FROM schema_migrations
      WHERE version = '009_harden_push_delivery.sql'
  );
