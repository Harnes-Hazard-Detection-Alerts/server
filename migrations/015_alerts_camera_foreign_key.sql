-- Alerts produced by the multi-camera pipeline always originate from a
-- registered camera. Refuse to guess or discard legacy source values: an
-- unmatched row aborts this transactional migration before the type change.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM alerts a
        LEFT JOIN cameras c ON c.id::text = a.source_id
        WHERE c.id IS NULL
    ) THEN
        RAISE EXCEPTION
            'alerts.source_id contains values that do not match cameras.id';
    END IF;
END
$$;

ALTER TABLE alerts
    RENAME COLUMN source_id TO camera_id;

ALTER TABLE alerts
    ALTER COLUMN camera_id TYPE uuid
    USING camera_id::uuid;

ALTER TABLE alerts
    ADD CONSTRAINT alerts_camera_fk
    FOREIGN KEY (camera_id)
    REFERENCES cameras(id)
    ON DELETE RESTRICT;

CREATE INDEX alerts_camera_occurred_idx
    ON alerts(camera_id, occurred_at_ms DESC, id DESC);
