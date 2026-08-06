"""titledb: the Nintendo title metadata subsystem.

`update` downloads the upstream JSON files and drives the refresh, `store` builds and
queries config/titles.db from them (schema in `schema`, migrations in `migrations/`).
"""
from titledb.update import update_titledb
