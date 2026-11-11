-- The collection: items, their passages and files; venues; members; the job
-- queue; the usage log. Vectors are half-precision, 256 dimensions, compared
-- by cosine distance.

CREATE EXTENSION IF NOT EXISTS vector;

-- Facts about the collection as a whole, e.g. the embedding model's revision.
CREATE TABLE meta (
    name   text PRIMARY KEY,
    value  text NOT NULL
);

CREATE TABLE members (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    key_hash    text NOT NULL UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz
);

CREATE TABLE venues (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          text NOT NULL,
    issns         text[] NOT NULL DEFAULT '{}',
    adapter       text,                 -- the source that harvests it: crossref | openalex
    source_id     text,                 -- the venue's identifier at that source
    publisher     text,
    category      text,                 -- Journals | Proceedings | Preprints | ...
    subscribed    boolean NOT NULL DEFAULT false,
    needs_filter  boolean NOT NULL DEFAULT false,
    marker        timestamptz           -- harvested up to this index date
);
CREATE UNIQUE INDEX venues_name ON venues (lower(name));

CREATE TABLE items (
    key                text PRIMARY KEY,
    citation_key       text,
    type               text NOT NULL,
    title              text NOT NULL,
    title_norm         text NOT NULL,   -- identity.normalise_title, for exact title lookups
    abstract           text,
    year               integer,
    date               text,
    venue_id           bigint REFERENCES venues,
    publisher          text,
    category           text,
    doi                text,
    arxiv_id           text,
    other_ids          jsonb NOT NULL DEFAULT '[]',  -- preprints this item stands for: ["doi:...", "arxiv:..."]
    creators           jsonb NOT NULL DEFAULT '[]',
    data               jsonb NOT NULL DEFAULT '{}',  -- the remaining bibliographic fields
    provenance_kind    text NOT NULL CHECK (provenance_kind IN ('venue', 'donation', 'activity')),
    provenance_member  bigint REFERENCES members,
    added_at           timestamptz NOT NULL DEFAULT now(),
    has_fulltext       boolean NOT NULL DEFAULT false,
    embedding          halfvec(256),
    tsv                tsvector GENERATED ALWAYS AS
                         (to_tsvector('simple', title || ' ' || coalesce(abstract, ''))) STORED
);
CREATE UNIQUE INDEX items_doi ON items (lower(doi)) WHERE doi IS NOT NULL;
CREATE UNIQUE INDEX items_arxiv_id ON items (lower(arxiv_id)) WHERE arxiv_id IS NOT NULL;
CREATE UNIQUE INDEX items_citation_key ON items (citation_key) WHERE citation_key IS NOT NULL;
CREATE INDEX items_title_norm ON items (title_norm);
CREATE INDEX items_other_ids ON items USING gin (other_ids jsonb_path_ops);
CREATE INDEX items_year ON items (year);
CREATE INDEX items_venue ON items (venue_id);
CREATE INDEX items_added_at ON items (added_at);
CREATE INDEX items_embedding ON items USING hnsw (embedding halfvec_cosine_ops);
CREATE INDEX items_tsv ON items USING gin (tsv);

CREATE TABLE item_creators (
    item      text NOT NULL REFERENCES items ON DELETE CASCADE,
    name_key  text NOT NULL,            -- identity.creator_keys
    position  integer NOT NULL,         -- the creator's place in the item's list, from 0
    PRIMARY KEY (item, position, name_key)
);
CREATE INDEX item_creators_name_key ON item_creators (name_key);

CREATE TABLE passages (
    item       text NOT NULL REFERENCES items ON DELETE CASCADE,
    idx        integer NOT NULL,        -- the passage's place in the paper, from 0
    part       text NOT NULL,           -- body | footnote | appendix
    pages      text NOT NULL DEFAULT '',
    text       text NOT NULL,
    embed_text text,                 -- the text without citation markers, where it differs
    artefact   boolean NOT NULL DEFAULT false,  -- not prose: stored, never embedded
    embedding  halfvec(256),
    tsv        tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
    PRIMARY KEY (item, idx)
);
CREATE INDEX passages_embedding ON passages USING hnsw (embedding halfvec_cosine_ops);
CREATE INDEX passages_tsv ON passages USING gin (tsv);

CREATE TABLE files (
    sha256      text PRIMARY KEY,
    item        text NOT NULL REFERENCES items ON DELETE CASCADE,
    bytes       bigint NOT NULL,
    donated_by  bigint REFERENCES members,
    donated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX files_item ON files (item);

-- Full texts that arrived without their PDF (the seed import): the PDF's
-- MD5 as the source recorded it, so the file is recognised when it comes.
CREATE TABLE pending_files (
    md5   text PRIMARY KEY,
    item  text NOT NULL REFERENCES items ON DELETE CASCADE
);

CREATE TABLE jobs (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind         text NOT NULL,
    payload      jsonb NOT NULL DEFAULT '{}',
    status       text NOT NULL DEFAULT 'waiting'
                   CHECK (status IN ('waiting', 'running', 'done', 'failed')),
    member       bigint REFERENCES members,
    created_at   timestamptz NOT NULL DEFAULT now(),
    started_at   timestamptz,
    finished_at  timestamptz,
    error        text,
    run_dir      text
);
CREATE INDEX jobs_waiting ON jobs (id) WHERE status = 'waiting';
CREATE INDEX jobs_member ON jobs (member, id);

CREATE TABLE usage_log (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at              timestamptz NOT NULL DEFAULT now(),
    member          bigint REFERENCES members,
    request         text NOT NULL,
    inputs          jsonb,
    reply           jsonb,
    status          integer NOT NULL,
    duration_ms     integer,
    client_version  text,
    agent           text
);
CREATE INDEX usage_log_member_at ON usage_log (member, at);
