-- What a finished job did, in one line (a failed job keeps its error):
-- donation progress shows it, e.g. "stored" or "already held".
ALTER TABLE jobs ADD COLUMN summary text;
