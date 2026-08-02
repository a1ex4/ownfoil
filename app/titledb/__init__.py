"""titledb: the Nintendo title metadata subsystem.

`source` downloads the upstream JSON files, `store` builds and queries config/titles.db from
them (schema in `schema`, migrations in `migrations/`), and `update` drives the two together.
"""
from titledb.update import update_titledb
