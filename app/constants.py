import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, 'data')
CONFIG_DIR = os.path.join(APP_DIR, 'config')

OWNFOIL_DB = 'sqlite:////' + CONFIG_DIR + '/ownfoil.db'

DEFAULT_SETTINGS = {
    "library": {
        "path": "/games",
        "region": "US",
        "language": "en"
    },
    "shop": {
        "motd": "Welcome to your own shop!",
        "encrypt": False,
        "clientCertPub": "-----BEGIN PUBLIC KEY-----",
        "clientCertKey": "-----BEGIN PRIVATE KEY-----"
    }
}

tinfoil_headers = [
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