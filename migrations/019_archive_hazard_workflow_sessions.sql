-- Preserve prior operator/HSE workflow before resetting an open camera hazard
-- for a new operating session. Camera lifecycle is not resolved by this reset.
CREATE TABLE alert_workflow_history (
    alert_id uuid NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    restarted_at_ms bigint NOT NULL,
    snapshot jsonb NOT NULL,
    PRIMARY KEY (alert_id, restarted_at_ms)
);
