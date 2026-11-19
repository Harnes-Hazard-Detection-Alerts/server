-- Jawaban operator saat bahaya masih berlangsung >= 10 detik (keputusan owner
-- 12 Nov 2026). Jawaban = CATATAN untuk HSE: tidak mengubah status alert —
-- `resolved` tetap ditentukan evaluasi kamera (CONTEXT "Arti status").
ALTER TABLE alerts
    ADD COLUMN operator_response text
        CHECK (operator_response IN ('worker_needed', 'worker_moving_away',
                                     'false_detection', 'needs_hse_help')),
    ADD COLUMN operator_responded_at_ms bigint
        CHECK (operator_responded_at_ms IS NULL OR operator_responded_at_ms > 0),
    ADD COLUMN operator_responded_by uuid REFERENCES users(id) ON DELETE SET NULL,
    ADD CONSTRAINT alerts_operator_response_pair
        CHECK ((operator_response IS NULL) = (operator_responded_at_ms IS NULL));
