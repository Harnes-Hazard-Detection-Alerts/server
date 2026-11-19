-- Nama tampilan excavator yang diatur HSE (keputusan owner 11 Nov 2026).
-- Tanpa baris = app memakai nama kamera. Nama bukan identitas: identitas
-- tetap equipment_instance_id dari tracking (CONTEXT P3).
CREATE TABLE equipment_labels (
    equipment_instance_id text PRIMARY KEY
        CHECK (length(equipment_instance_id) BETWEEN 1 AND 200),
    display_name text NOT NULL
        CHECK (length(btrim(display_name)) BETWEEN 1 AND 100),
    updated_at_ms bigint NOT NULL,
    updated_by uuid REFERENCES users(id) ON DELETE SET NULL
);
