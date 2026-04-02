"""
Verifies that init_db() produces the expected database schema.

Cross-branch workflow
---------------------
1. On develop (or after intentional schema changes):
       pytest --update-schema
   Commit the updated tests/fixtures/db_schema.json.

2. On any feature branch:
       pytest tests/test_db_schema.py
   Fails if the initialized schema diverges from the committed baseline.
"""

import json
import pytest
from pathlib import Path
from sqlalchemy import create_engine, inspect as sa_inspect

SCHEMA_FIXTURE = Path(__file__).parent / 'fixtures' / 'db_schema.json'


def extract_schema(engine):
    """Return a normalized, sorted representation of all tables (excluding alembic_version)."""
    inspector = sa_inspect(engine)
    schema = {}
    for table in sorted(inspector.get_table_names()):
        if table == 'alembic_version':
            continue
        schema[table] = {
            'columns': sorted(
                [
                    {
                        'name': c['name'],
                        'type': str(c['type']),
                        'nullable': c['nullable'],
                    }
                    for c in inspector.get_columns(table)
                ],
                key=lambda c: c['name'],
            ),
            'foreign_keys': sorted(
                [
                    {
                        'constrained': fk['constrained_columns'],
                        'referred_table': fk['referred_table'],
                        'referred_columns': fk['referred_columns'],
                    }
                    for fk in inspector.get_foreign_keys(table)
                ],
                key=str,
            ),
            'unique_constraints': sorted(
                [sorted(uc['column_names']) for uc in inspector.get_unique_constraints(table)],
                key=str,
            ),
        }
    return schema


def test_db_init_schema(tmp_path, monkeypatch, request):
    """
    init_db() on a fresh database must produce the schema committed in
    tests/fixtures/db_schema.json (the develop baseline).
    """
    import db as db_module
    from app import create_app, init_db

    db_path = str(tmp_path / 'ownfoil.db')
    db_uri = f'sqlite:///{db_path}'

    # Redirect init_db's os.path.exists(DB_FILE) check to the temp path.
    # DB_FILE is imported into db.py's namespace via `from constants import *`.
    monkeypatch.setattr(db_module, 'DB_FILE', db_path)

    # Pass the temp URI directly so db.init_app() (called inside create_app) receives it
    # before Flask-SQLAlchemy eagerly creates the engine.
    flask_app = create_app(db_uri=db_uri)

    init_db(flask_app)

    schema = extract_schema(create_engine(db_uri))

    update = request.config.getoption('--update-schema')

    if update or not SCHEMA_FIXTURE.exists():
        SCHEMA_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        SCHEMA_FIXTURE.write_text(json.dumps(schema, indent=2) + '\n')
        action = 'updated' if update else 'created'
        pytest.skip(f'Schema fixture {action} — commit {SCHEMA_FIXTURE.relative_to(Path.cwd())}')

    expected = json.loads(SCHEMA_FIXTURE.read_text())
    assert schema == expected, (
        'DB schema diverged from the develop baseline.\n'
        'If this is intentional, run `pytest --update-schema` from develop and commit the fixture.'
    )
