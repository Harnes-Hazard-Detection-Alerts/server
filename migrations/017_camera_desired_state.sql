-- Intent per kamera (kernel supervisor, keputusan owner 14 Nov 2026):
-- desired_on = "kamera ini HARUS menghasilkan frame". Start/stop HSE
-- menulis intent; boot dan supervisor loop di CameraManager membuat runtime
-- mengejar intent — restart server atau produser telat tidak lagi
-- meninggalkan status "online" basi / gejala unknown-connecting di app.
-- Default true: kamera terdaftar = dimaksudkan jalan (konsisten dengan
-- auto-start saat registrasi, 13 Nov).
ALTER TABLE cameras ADD COLUMN desired_on boolean NOT NULL DEFAULT true;
