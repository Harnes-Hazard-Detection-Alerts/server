-- Nomor telepon iPhone kabin per excavator (keputusan owner 11 Nov 2026):
-- HSE menelepon kabin excavator, bukan nomor pribadi operator. Disimpan di
-- baris yang sama dengan nama excavator; salah satu boleh kosong.
ALTER TABLE equipment_labels ALTER COLUMN display_name DROP NOT NULL;
ALTER TABLE equipment_labels
    ADD COLUMN cabin_phone text
        CHECK (cabin_phone IS NULL OR length(btrim(cabin_phone)) BETWEEN 3 AND 30);
ALTER TABLE equipment_labels
    ADD CONSTRAINT equipment_labels_not_empty
        CHECK (display_name IS NOT NULL OR cabin_phone IS NOT NULL);
