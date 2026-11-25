-- Keyword search per field (the `text` filter, retrieval.where): an index
-- for each place the words can be looked for. The expressions must stay
-- the same as in retrieval.KEYWORD_FIELDS, or the indexes go unused.
-- Authors are matched in the creators' names as stored (every string of
-- the creators list); venues are few and need no index.
CREATE INDEX items_title_words ON items USING gin (to_tsvector('simple', title));
CREATE INDEX items_abstract_words ON items USING gin (to_tsvector('simple', coalesce(abstract, '')));
CREATE INDEX items_author_words ON items USING gin (to_tsvector('simple', creators));
CREATE INDEX items_all_words ON items USING gin ((tsv || to_tsvector('simple', creators)));
