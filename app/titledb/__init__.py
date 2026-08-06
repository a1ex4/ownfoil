"""titledb: the Nintendo title metadata subsystem.

`update` downloads the upstream JSON files and drives the refresh, `store` builds and
queries config/titles.db from them (schema in `schema`). Metadata can come from several
sources - the download, user edits, extraction from the files - which `store` merges field
by field following `schema.SOURCE_PRIORITY`; everything but the download lives durably in
ownfoil.db (`db.title_overrides`) and is projected into titles.db on each rebuild.
"""
from titledb.update import update_titledb
