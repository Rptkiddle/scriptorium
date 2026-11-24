-- Admins: members who may use the admin menu (the client's admin tools).
-- The mark is set on the host only (`scriptorium members admin <name>`).
ALTER TABLE members ADD COLUMN admin boolean NOT NULL DEFAULT false;

-- Every change made through the admin menu: who, what, and the value
-- before and after. A new member's key is never written here.
CREATE TABLE admin_log (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at      timestamptz NOT NULL DEFAULT now(),
    member  bigint REFERENCES members,
    action  text NOT NULL,          -- e.g. settings.set, members.revoke, runs.start
    target  text,                   -- the setting, member, venue, run kind or job
    before  jsonb,
    after   jsonb
);
