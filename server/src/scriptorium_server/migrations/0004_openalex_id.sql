-- The OpenAlex work an item was harvested from, so a harvest run again
-- recognises papers it has added before, including papers without a DOI.
ALTER TABLE items ADD COLUMN openalex_id text;
CREATE UNIQUE INDEX items_openalex_id ON items (openalex_id) WHERE openalex_id IS NOT NULL;

-- A new item's citation key is checked against the keys that start the
-- same way (citation_key LIKE 'smith2021%'); without an index for prefix
-- matching that reads the whole table at every insert.
CREATE INDEX items_citation_key_prefix ON items (citation_key text_pattern_ops);
