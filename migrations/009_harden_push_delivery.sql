-- M7 hardening: satu APNs token hanya boleh dimiliki satu akun, dan setiap
-- percobaan push harus menunjuk baris device_tokens yang benar.
--
-- Unique index sengaja tidak melakukan deduplikasi diam-diam. Jika instalasi
-- lama memiliki token ganda, migrasi berhenti agar operator dapat meninjau
-- kepemilikan token sebelum melanjutkan.
CREATE UNIQUE INDEX device_tokens_push_token_unique_idx
    ON device_tokens(push_token);

ALTER TABLE alert_deliveries
    ADD COLUMN push_device_id uuid REFERENCES device_tokens(id) ON DELETE SET NULL,
    ADD COLUMN channel text NOT NULL DEFAULT 'live'
        CHECK (channel IN ('live', 'push'));

ALTER TABLE alert_deliveries ALTER COLUMN channel DROP DEFAULT;

CREATE INDEX alert_deliveries_push_device_idx
    ON alert_deliveries(push_device_id);
