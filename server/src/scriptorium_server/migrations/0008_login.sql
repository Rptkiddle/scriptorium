-- Login through an OpenID Connect provider (the settings login.*).
-- A member who logs in is known by the provider's address (issuer) and
-- the provider's identifier for the person (subject, the token's `sub`).
-- Such a member needs neither a name nor a key: the server creates the
-- row at the first login and stores nothing else about the person.
ALTER TABLE members ALTER COLUMN name DROP NOT NULL;
ALTER TABLE members ALTER COLUMN key_hash DROP NOT NULL;
ALTER TABLE members ADD COLUMN issuer text;
ALTER TABLE members ADD COLUMN subject text;
CREATE UNIQUE INDEX members_login ON members (issuer, subject) WHERE subject IS NOT NULL;
