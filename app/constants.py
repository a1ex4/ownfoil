CONFIG_DIR = './config'
DATA_DIR = './data'

OWNFOIL_DB = 'ownfoil.db'

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