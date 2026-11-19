-- Expand device_tokens so one operator can register one iPhone token and one
-- Apple Watch token. Existing rows remain iPhone targets for compatibility.
ALTER TABLE device_tokens
    ADD COLUMN target text NOT NULL DEFAULT 'iphone'
    CHECK (target IN ('iphone', 'watch'));

CREATE UNIQUE INDEX device_tokens_operator_target_unique_idx
    ON device_tokens(operator_id, target);
