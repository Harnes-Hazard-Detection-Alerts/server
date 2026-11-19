CREATE TABLE sessions (
    token_hash text PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL CHECK (expires_at > created_at)
);
CREATE INDEX sessions_user_id_idx ON sessions(user_id);
CREATE INDEX sessions_expires_at_idx ON sessions(expires_at);
