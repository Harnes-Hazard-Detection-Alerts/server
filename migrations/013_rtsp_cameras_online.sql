-- Kamera RTSP menjadi sumber live sungguhan (api-draft 4.3, dieksekusi 11 Nov 2026).
-- CHECK lama memaksa type rtsp selalu offline (placeholder 501). Kini start()
-- menjalankan stream live: status boleh online saat berjalan / error bila putus.
DO $$
DECLARE rogue text;
BEGIN
    FOR rogue IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'cameras'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) ILIKE '%rtsp%'
    LOOP
        EXECUTE format('ALTER TABLE cameras DROP CONSTRAINT %I', rogue);
    END LOOP;
END $$;
