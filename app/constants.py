import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('OWNFOIL_DATA_DIR') or os.path.join(APP_DIR, 'data')
CONFIG_DIR = os.environ.get('OWNFOIL_CONFIG_DIR') or os.path.join(APP_DIR, 'config')
DB_FILE = os.path.join(CONFIG_DIR, 'ownfoil.db')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'settings.yaml')
KEYS_FILE = os.path.join(CONFIG_DIR, 'keys.txt')
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
            "verification": {
                "enabled": True,
                "depth": "hash",
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
        "titledb_update_interval": "12h",
    },
    "worker": {
        "count": 2,
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
COMPRESS_EXT = {'nsp': 'nsz', 'xci': 'xcz'}
DECOMPRESS_EXT = {v: k for k, v in COMPRESS_EXT.items()}

# Filesystems that native OS watchers cannot reliably observe (server-side changes
# emit no inotify/FSEvents), so paths on these must be polled.
NETWORK_FSTYPES = {
    'nfs', 'nfs4', 'cifs', 'smbfs', 'smb3', 'smb2', 'afs', 'ncpfs', '9p',
    'fuse.sshfs', 'fuse.rclone', 'fuse.glusterfs', 'fuse.cephfs', 'ceph',
    'glusterfs', 'lustre', 'beegfs',
}

# GetDriveTypeW return codes that mean a locally attached volume, which
# ReadDirectoryChangesW can watch natively. Anything else (DRIVE_UNKNOWN,
# DRIVE_NO_ROOT_DIR, DRIVE_REMOTE) is polled instead.
WINDOWS_LOCAL_DRIVE_TYPES = {
    2,  # DRIVE_REMOVABLE
    3,  # DRIVE_FIXED
    5,  # DRIVE_CDROM
    6,  # DRIVE_RAMDISK
}

APP_TYPE_BASE = 'BASE'
APP_TYPE_UPD = 'UPDATE'
APP_TYPE_DLC = 'DLC'
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

# OS-specific restricted characters, mapped to their full-width equivalents
RESTRICTED_CHARS_WINDOWS = {
    '/': '／', '\\': '＼', ':': '：', '*': '＊', '?': '？',
    '"': '＂', '<': '＜', '>': '＞', '|': '｜'
}
RESTRICTED_CHARS_UNIX = {'/': '／'}
# A Windows name cannot end with a period, only replaced as the last character
TRAILING_DOT_WINDOWS = '．'

# Windows path length limits, one char shorter than the documented values
# to account for the terminating NUL
MAX_PATH_WINDOWS = 259
MAX_DIR_PATH_WINDOWS = 247 # Directories need room for a 8.3 filename
MAX_PART_WINDOWS = 255
MIN_PART_WINDOWS = 8 # Never truncate a name below this
TRUNCATION_MARKER = '…' # Marks a name that had to be shortened
TEMPLATE_NAME_KEYS = ('titleName', 'appName') # Organizer template values shortened to fit a path
# Leaves room for a library path of ~60 characters and shortens only 0.4% of known titles.
MAX_NAME_WINDOWS = 80
COLLISION_SUFFIX_RESERVE = 4 # Room for the "(n)" suffix added on filename collisions

# Reserved names on Windows
RESERVED_NAMES_WINDOWS = {
    'con', 'prn', 'aux', 'nul', 'com1', 'com2', 'com3', 'com4', 'com5', 'com6', 'com7', 'com8', 'com9',
    'lpt1', 'lpt2', 'lpt3', 'lpt4', 'lpt5', 'lpt6', 'lpt7', 'lpt8', 'lpt9'
}