-- Signing in to the web page through the login provider (login.issuer).
-- The member is found or made by the provider's subject, as for the
-- client (0008_login); these tables hold only the sign-in itself.

-- A sign-in under way: what the browser must bring back to /auth/callback.
CREATE TABLE web_logins (
    state       text PRIMARY KEY,
    nonce       text NOT NULL,
    verifier    text NOT NULL,          -- the PKCE code verifier
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- A signed-in browser: the hash of its session cookie. The person's name,
-- from the provider's ID token, is kept here for the page's header only,
-- and goes when the session ends; the members table holds no name of a
-- member who logs in.
CREATE TABLE web_sessions (
    token_hash  text PRIMARY KEY,
    member      bigint NOT NULL REFERENCES members ON DELETE CASCADE,
    shown_name  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL
);
CREATE INDEX web_sessions_member ON web_sessions (member);
