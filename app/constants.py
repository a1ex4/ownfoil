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
TITLES_DB_FILE = os.path.join(CONFIG_DIR, 'titles.db')
CUSTOM_TITLES_FILE = os.path.join(CONFIG_DIR, 'custom_titles.json')
OWNFOIL_DB = 'sqlite:///' + DB_FILE

# Global file watcher defaults
DEFAULT_WATCHER = {"enabled": True, "polling_interval": 60}

DEFAULT_SETTINGS = {
    "library": {
        "paths": ["/games"],
        "watcher": dict(DEFAULT_WATCHER),
        "management": {
            "compression": {
                "enabled": False,
                "level": 18,
                "long_distance": False,
                "mode": "auto",
                "block_size_exponent": 20,
                "threads": 0,
            },
            "delete_older_updates": False,
            "organizer": {
                "enabled": False,
                "remove_empty_folders": False,
                "windows_compatible": False,
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
        "host": "",
        "public": False,
        "motd": "Welcome to your own shop!",
        "clients": {
            "cyberfoil": {
                "enabled": True,
                "hauth": {},
            },
            "tinfoil": {
                "enabled": True,
                "encrypt": True,
                "clientCertPub": "-----BEGIN PUBLIC KEY-----",
                "clientCertKey": "-----BEGIN PRIVATE KEY-----",
                "hauth": {},
            },
            "sphaira": {"enabled": True,}
        }
    },
    "scheduler": {
        "scan_interval": "12h",
    },
    "worker": {
        "count": 2,
        # Per-concurrency-group cap: at most N tasks of a group run at once, regardless of
        # worker count. 'io' holds the disk-heavy (de)compression/verify tasks — default 1 to
        # avoid seek-thrash from parallel multi-GB reads on a single disk.
        "group_limits": {
            "io": 1,
        },
    }
}


ALLOWED_EXTENSIONS = [
    'nsp',
    'nsz',
    'xci',
    'xcz',
]

# Filesystems that native OS watchers cannot reliably observe (server-side changes
# emit no inotify/FSEvents), so paths on these must be polled.
NETWORK_FSTYPES = {
    'nfs', 'nfs4', 'cifs', 'smbfs', 'smb3', 'smb2', 'afs', 'ncpfs', '9p',
    'fuse.sshfs', 'fuse.rclone', 'fuse.glusterfs', 'fuse.cephfs', 'ceph',
    'glusterfs', 'lustre', 'beegfs',
}

APP_TYPE_BASE = 'BASE'
APP_TYPE_UPD = 'UPDATE'
APP_TYPE_DLC = 'DLC'

# File compression (nsz): uncompressed -> compressed extension mapping and back.
COMPRESS_EXT = {'nsp': 'nsz', 'xci': 'xcz'}
DECOMPRESS_EXT = {v: k for k, v in COMPRESS_EXT.items()}
APP_TYPE_MAP = {
    128: APP_TYPE_BASE,
    129: APP_TYPE_UPD,
    130: APP_TYPE_DLC
}

APP_TYPE_FILTERS = {
    'base': APP_TYPE_BASE,
    'update': APP_TYPE_UPD,
    'dlc': APP_TYPE_DLC,
    'multi': 'MULTI'
}

# Define OS-specific forbidden characters for Organizer
FORBIDDEN_CHARS_WINDOWS = set('<>:"/\\|?*')
FORBIDDEN_CHARS_UNIX = set('/') # Only / is truly forbidden on Unix-like systems

# Reserved names on Windows
RESERVED_NAMES_WINDOWS = {
    'con', 'prn', 'aux', 'nul', 'com1', 'com2', 'com3', 'com4', 'com5', 'com6', 'com7', 'com8', 'com9',
    'lpt1', 'lpt2', 'lpt3', 'lpt4', 'lpt5', 'lpt6', 'lpt7', 'lpt8', 'lpt9'
}