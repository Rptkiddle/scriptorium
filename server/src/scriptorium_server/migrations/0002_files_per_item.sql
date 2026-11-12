-- One PDF can be the full text of more than one item (a chapter and the
-- book it is part of; one publication recorded twice). Stored files and
-- awaited files are therefore kept per file AND item; the file itself is
-- stored once, by its content hash.
ALTER TABLE pending_files DROP CONSTRAINT pending_files_pkey;
ALTER TABLE pending_files ADD PRIMARY KEY (md5, item);

ALTER TABLE files DROP CONSTRAINT files_pkey;
ALTER TABLE files ADD PRIMARY KEY (sha256, item);

-- A stored file has its annotations and file metadata removed, so its
-- bytes differ from the file that arrived. The MD5 of the file as it
-- arrived is kept: the stored parse (TEI) and the seed's awaited files
-- know a PDF by it.
ALTER TABLE files ADD COLUMN source_md5 text;
CREATE INDEX files_source_md5 ON files (source_md5);

-- Items and passages still waiting for a vector: the worker looks for them
-- whenever its queue is empty, so the usual answer ("none") must be quick.
CREATE INDEX items_without_vector ON items (key) WHERE embedding IS NULL;
CREATE INDEX passages_without_vector ON passages (item, idx)
    WHERE embedding IS NULL AND NOT artefact;
