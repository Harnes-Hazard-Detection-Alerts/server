-- Operator response, HSE claim/validation and the 10-second hazard milestone.
-- Kept separate from migration 017 so an already-applied checksum is preserved.
ALTER TABLE alerts
    ADD COLUMN hazard_active_10s_at_ms bigint
        CHECK (hazard_active_10s_at_ms IS NULL OR hazard_active_10s_at_ms > 0),
    ADD COLUMN hse_on_my_way_at_ms bigint
        CHECK (hse_on_my_way_at_ms IS NULL OR hse_on_my_way_at_ms > 0),
    ADD COLUMN hse_on_my_way_by uuid REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN hse_on_my_way_name text,
    ADD COLUMN hse_confirmed_safe_at_ms bigint
        CHECK (hse_confirmed_safe_at_ms IS NULL OR hse_confirmed_safe_at_ms > 0),
    ADD COLUMN hse_confirmed_safe_by uuid REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN hse_confirmed_safe_name text,
    ADD CONSTRAINT alerts_hse_claim_pair
        CHECK ((hse_on_my_way_at_ms IS NULL) = (hse_on_my_way_name IS NULL)),
    ADD CONSTRAINT alerts_hse_confirmation_pair
        CHECK ((hse_confirmed_safe_at_ms IS NULL) = (hse_confirmed_safe_name IS NULL));
