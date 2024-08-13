import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, 'data')
CONFIG_DIR = os.path.join(APP_DIR, 'config')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'settings.yaml')
KEYS_FILE = os.path.join(CONFIG_DIR, 'keys.txt')
TITLEDB_DIR = os.path.join(DATA_DIR, 'titledb')
TITLEDB_URL = 'https://github.com/blawar/titledb.git'
TITLEDB_ARTEFACTS_URL = 'https://nightly.link/a1ex4/ownfoil/workflows/region_titles/master/titledb.zip'
TITLEDB_DEFAULT_FILES = [
    'cnmts.json',
    'versions.json',
    'versions.txt',
    'languages.json',
]

OWNFOIL_DB = 'sqlite:////' + os.path.join(CONFIG_DIR, 'ownfoil.db')

DEFAULT_SETTINGS = {
    "library": {
        "paths": ["/games"],
    },
    "titles": {
        "language": "en",
        "region": "US",
        "valid_keys": False,
    },
    "shop": {
        "motd": "Welcome to your own shop!",
        "public": False,
        "encrypt": False,
        "clientCertPub": "-----BEGIN PUBLIC KEY-----",
        "clientCertKey": "-----BEGIN PRIVATE KEY-----"
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