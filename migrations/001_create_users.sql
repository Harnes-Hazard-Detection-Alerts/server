CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username text NOT NULL CHECK (username = btrim(username) AND length(username) BETWEEN 1 AND 100),
    display_name text NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 200),
    password_hash text NOT NULL CHECK (length(password_hash) > 0),
    role text NOT NULL CHECK (role IN ('operator', 'hse')),
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_username_unique ON users (lower(username));

CREATE FUNCTION set_user_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$$;
CREATE TRIGGER users_updated_at BEFORE UPDATE ON users
FOR EACH ROW EXECUTE FUNCTION set_user_updated_at();
