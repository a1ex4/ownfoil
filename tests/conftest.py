import sys
from pathlib import Path

# nsz (used by settings.py) calls parser.parse_args() at import time and will
# exit(2) if it sees unrecognised flags like pytest's own CLI arguments.
# Reset argv to a bare invocation before any app module is imported.
sys.argv = ['ownfoil']

# Make app/ importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent / 'app'))


def pytest_addoption(parser):
    parser.addoption(
        '--update-schema',
        action='store_true',
        default=False,
        help='Regenerate tests/fixtures/db_schema.json from the current branch (run from develop).',
    )
    parser.addoption(
        '--update-content',
        action='store_true',
        default=False,
        help='Regenerate tests/fixtures/db_content.json from the current branch (run from develop).',
    )
