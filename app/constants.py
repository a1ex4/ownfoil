import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(APP_DIR)
NSZ_DIR = os.path.join(PROJECT_DIR, 'nsz')
NSZ_SCRIPT = os.path.join(NSZ_DIR, 'nsz.py')
DATA_DIR = os.path.join(APP_DIR, 'data')
CONFIG_DIR = os.path.join(APP_DIR, 'config')
DB_FILE = os.path.join(CONFIG_DIR, 'ownfoil.db')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'settings.yaml')
KEYS_FILE = os.path.join(CONFIG_DIR, 'keys.txt')
CACHE_DIR = os.path.join(DATA_DIR, 'cache')
LIBRARY_CACHE_FILE = os.path.join(CACHE_DIR, 'library.json')
SHOP_SECTIONS_CACHE_FILE = os.path.join(CACHE_DIR, 'shop_sections.json')
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
    },
    "titles": {
        "language": "en",
        "region": "US",
        "valid_keys": False,
    },
    "downloads": {
        "enabled": False,
        "interval_minutes": 60,
        "min_seeders": 2,
        "required_terms": ["update"],
        "blacklist_terms": [],
        "search_prefix": "Nintendo Switch",
        "search_suffix": "update",
        "prowlarr": {
            "url": "",
            "api_key": "",
            "indexer_ids": []
        },
        "torrent_client": {
            "type": "qbittorrent",
            "url": "",
            "username": "",
            "password": "",
            "category": "ownfoil",
            "download_path": ""
        }
    },
    "shop": {
        "motd": "Welcome to your own shop!",
        "public": False,
        "encrypt": True,
        "public_key": "",
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
