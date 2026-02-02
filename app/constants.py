import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, 'data')
CONFIG_DIR = os.path.join(APP_DIR, 'config')
DB_FILE = os.path.join(CONFIG_DIR, 'ownfoil.db')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'settings.yaml')
KEYS_FILE = os.path.join(CONFIG_DIR, 'keys.txt')
CACHE_DIR = os.path.join(DATA_DIR, 'cache')
LIBRARY_CACHE_FILE = os.path.join(CACHE_DIR, 'library.json')
ALEMBIC_DIR = os.path.join(APP_DIR, 'migrations')
ALEMBIC_CONF = os.path.join(ALEMBIC_DIR, 'alembic.ini')
TITLEDB_DIR = os.path.join(DATA_DIR, 'titledb')
TITLEDB_URL = 'https://github.com/blawar/titledb.git'
TITLEDB_ARTEFACTS_URL = 'https://nightly.link/a1ex4/ownfoil/workflows/region_titles/master/titledb.zip'
TITLEDB_DEFAULT_FILES = [
    'cnmts.json',
    'versions.json',
    'versions.txt',
    'languages.json',
]

OWNFOIL_DB = 'sqlite:///' + DB_FILE

DEFAULT_SETTINGS = {
    "library": {
        "paths": ["/games"],
        "management": {
            "compress_files": False,
            "delete_older_updates": False,
            "organizer": {
                "enabled": False,
                "remove_empty_folders": False,
                "templates": {
                    "base": "{titleName}/{titleName} [{appId}][v{appVersion}]",
                    "update": "{titleName}/{titleName} [{appId}][v{appVersion}]",
                    "dlc": "{titleName}/{appName} [{appId}][v{appVersion}]",
                    "multi": "{titleName}/{titleName} [{titleId}]"
                }
            }
        }
    },
    "titles": {
        "language": "en",
        "region": "US",
    },
    "shop": {
        "motd": "Welcome to your own shop!",
        "public": False,
        "encrypt": True,
        "clientCertPub": "-----BEGIN PUBLIC KEY-----",
        "clientCertKey": "-----BEGIN PRIVATE KEY-----",
        "host": "",
        "hauth": "",
    }
}

TINFOIL_HEADERS = [
    'Theme',
    'Uid',
    'Version',
    'Revision',
    'Language',
    'Hauth',
    'Uauth'
]

ALLOWED_EXTENSIONS = [
    'nsp',
    'nsz',
    'xci',
    'xcz',
]

APP_TYPE_BASE = 'BASE'
APP_TYPE_UPD = 'UPDATE'
APP_TYPE_DLC = 'DLC'
APP_TYPE_MAP = {
    128: APP_TYPE_BASE,
    129: APP_TYPE_UPD,
    130: APP_TYPE_DLC
}

# Define OS-specific forbidden characters for Organizer
FORBIDDEN_CHARS_WINDOWS = set('<>:"/\\|?*')
FORBIDDEN_CHARS_UNIX = set('/') # Only / is truly forbidden on Unix-like systems

# Reserved names on Windows
RESERVED_NAMES_WINDOWS = {
    'con', 'prn', 'aux', 'nul', 'com1', 'com2', 'com3', 'com4', 'com5', 'com6', 'com7', 'com8', 'com9',
    'lpt1', 'lpt2', 'lpt3', 'lpt4', 'lpt5', 'lpt6', 'lpt7', 'lpt8', 'lpt9'
}