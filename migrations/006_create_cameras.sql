-- Kamera sebagai resource (api-draft 4.3; P2: recording sekarang, rtsp placeholder
-- yang selalu menolak start dengan 501 sampai dieksekusi). Kamera = sumber frame
-- yang diproses server; iOS hanya mengonsumsi (P1).
CREATE TABLE cameras (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 100),
    location text CHECK (location IS NULL OR length(btrim(location)) BETWEEN 1 AND 100),
    type text NOT NULL DEFAULT 'recording' CHECK (type IN ('recording', 'rtsp')),
    status text NOT NULL DEFAULT 'offline' CHECK (status IN ('online', 'offline', 'error')),
    source text NOT NULL CHECK (length(btrim(source)) BETWEEN 1 AND 400),
    playback_fps integer CHECK (playback_fps IS NULL OR playback_fps BETWEEN 1 AND 60),
    loop boolean NOT NULL DEFAULT false,
    last_seen_ms bigint,
    health_note text,
    registered_at_ms bigint NOT NULL CHECK (registered_at_ms > 0),
    CHECK (type <> 'rtsp' OR status = 'offline')
);
CREATE UNIQUE INDEX cameras_source_unique ON cameras (source);
CREATE INDEX cameras_status_idx ON cameras (status);
