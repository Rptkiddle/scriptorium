-- The code names a member's PDF handed to the server a PDF upload: the files
-- table's columns and the items' provenance value follow.
ALTER TABLE files RENAME COLUMN donated_by TO uploaded_by;
ALTER TABLE files RENAME COLUMN donated_at TO uploaded_at;
ALTER TABLE items DROP CONSTRAINT items_provenance_kind_check;
UPDATE items SET provenance_kind = 'pdf_upload' WHERE provenance_kind = 'donation';
ALTER TABLE items ADD CONSTRAINT items_provenance_kind_check
    CHECK (provenance_kind IN ('venue', 'pdf_upload', 'activity'));
