-- Collections: a member's own lists of items. A history collection is
-- made by a workflow run and keeps what was asked; an active collection is
-- made and named by the member. An item's labels are the names of the
-- collections that hold it.
CREATE TABLE collections (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    member      bigint NOT NULL REFERENCES members,
    kind        text NOT NULL CHECK (kind IN ('history', 'active')),
    name        text NOT NULL,
    workflow    text,                 -- history: search | discovery | interrogation | ...
    request     jsonb,                -- history: what was asked (query, filters, question)
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX collections_member ON collections (member, kind, created_at DESC);
-- a member's active collections are told apart by name
CREATE UNIQUE INDEX collections_active_name ON collections (member, lower(name))
    WHERE kind = 'active';

CREATE TABLE collection_items (
    collection  bigint NOT NULL REFERENCES collections ON DELETE CASCADE,
    item        text NOT NULL REFERENCES items ON DELETE CASCADE,
    rank        integer,              -- history: the item's place in the result, from 1
    added_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (collection, item)
);
CREATE INDEX collection_items_item ON collection_items (item);

-- The code version that served each request, so a result can be tied to
-- the code that ranked it.
ALTER TABLE usage_log ADD COLUMN server_commit text;
