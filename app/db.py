from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.sqlite import insert
from flask_migrate import Migrate, upgrade
from alembic.runtime.migration import MigrationContext
from alembic.config import Config
from alembic.script import ScriptDirectory
from flask_login import UserMixin
from alembic import command
import os, sys
import shutil
import logging
import datetime
from constants import *
from utils import throttle

# Retrieve main logger
logger = logging.getLogger('main')

db = SQLAlchemy()
migrate = Migrate()

# Alembic functions
def get_alembic_cfg():
    cfg = Config(ALEMBIC_CONF)
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    return cfg

def get_current_db_version():
    engine = create_engine(OWNFOIL_DB)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        current_rev = context.get_current_revision()
        return current_rev or '0'
    
def create_db_backup():
    current_revision = get_current_db_version()
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f".backup_v{current_revision}_{timestamp}.db"
    backup_path = os.path.join(CONFIG_DIR, backup_filename)
    shutil.copy2(DB_FILE, backup_path)
    logger.info(f"Database backup created: {backup_path}")
    
def is_migration_needed():
    alembic_cfg = get_alembic_cfg()
    script = ScriptDirectory.from_config(alembic_cfg)
    latest_revision = script.get_current_head()
    current_revision = get_current_db_version()
    if current_revision != latest_revision:
        logger.info(f'Database migration needed, from {current_revision} to {latest_revision}')
        return True
    else:
        logger.info(f"Database version is up to date ({current_revision})")
        return False

def to_dict(db_results):
    return {c.name: getattr(db_results, c.name) for c in db_results.__table__.columns}

class Libraries(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    path = db.Column(db.String, unique=True, nullable=False)
    last_scan = db.Column(db.DateTime)

class Files(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    library_id = db.Column(db.Integer, db.ForeignKey('libraries.id', ondelete="CASCADE"), nullable=False)
    filepath = db.Column(db.String, unique=True, nullable=False)
    folder = db.Column(db.String)
    filename = db.Column(db.String, nullable=False)
    extension = db.Column(db.String)
    size = db.Column(db.Integer)
    compressed = db.Column(db.Boolean, default=False)
    multicontent = db.Column(db.Boolean, default=False)
    nb_content = db.Column(db.Integer, default=0)
    download_count = db.Column(db.Integer, default=0)
    identified = db.Column(db.Boolean, default=False)
    identification_type = db.Column(db.String)
    identification_error = db.Column(db.String)
    identification_attempts = db.Column(db.Integer, default=0)
    last_attempt = db.Column(db.DateTime, default=datetime.datetime.now())
    mtime = db.Column(db.Float)

    library = db.relationship('Libraries', backref=db.backref('files', lazy=True, cascade="all, delete-orphan"))

class Titles(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title_id = db.Column(db.String, unique=True)
    have_base = db.Column(db.Boolean, default=False)
    up_to_date = db.Column(db.Boolean, default=False)
    complete = db.Column(db.Boolean, default=False)

# Association table for many-to-many relationship between Apps and Files
app_files = db.Table('app_files',
    db.Column('app_id', db.Integer, db.ForeignKey('apps.id', ondelete="CASCADE"), primary_key=True),
    db.Column('file_id', db.Integer, db.ForeignKey('files.id', ondelete="CASCADE"), primary_key=True)
)

class Apps(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title_id = db.Column(db.Integer, db.ForeignKey('titles.id', ondelete="CASCADE"), nullable=False)
    app_id = db.Column(db.String)
    app_version = db.Column(db.String)
    app_type = db.Column(db.String)
    owned = db.Column(db.Boolean, default=False)

    title = db.relationship('Titles', backref=db.backref('apps', lazy=True, cascade="all, delete-orphan"))
    files = db.relationship('Files', secondary=app_files, backref=db.backref('apps', lazy='select'))

    __table_args__ = (db.UniqueConstraint('app_id', 'app_version', name='uq_apps_app_version'),)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user = db.Column(db.String(100), unique=True)
    password = db.Column(db.String(100))
    admin_access = db.Column(db.Boolean)
    shop_access = db.Column(db.Boolean)
    backup_access = db.Column(db.Boolean)

    @property
    def is_admin(self):
        return self.admin_access

    def has_shop_access(self):
        return self.shop_access

    def has_backup_access(self):
        return self.backup_access
    
    def has_admin_access(self):
        return self.admin_access

    def has_access(self, access):
        if access == 'admin':
            return self.has_admin_access()
        elif access == 'shop':
            return self.has_shop_access()
        elif access == 'backup':
            return self.has_backup_access()

class Task(db.Model):
    __tablename__ = 'tasks'

    id = db.Column(db.Integer, primary_key=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('tasks.id'), nullable=True)
    task_name = db.Column(db.String, nullable=False, index=True)
    status = db.Column(db.String, nullable=False, default='pending')
    completion_pct = db.Column(db.Integer, default=0)
    input_json = db.Column(db.Text, nullable=False, default='{}')
    input_hash = db.Column(db.String(64), nullable=False)
    output_json = db.Column(db.Text)
    exit_code = db.Column(db.Integer)
    error_message = db.Column(db.Text)
    run_after = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)

    children = db.relationship('Task', backref=db.backref('parent', remote_side=[id]), lazy='dynamic')

    __table_args__ = (
        db.Index('ix_tasks_status_created', 'status', 'created_at'),
        db.Index('ix_tasks_parent_id', 'parent_id'),
    )


class IgnoredEvent(db.Model):
    """File events the watcher should ignore (written by worker before move/delete)."""
    __tablename__ = 'ignored_events'
    id = db.Column(db.Integer, primary_key=True)
    src_path = db.Column(db.String, nullable=False)
    dest_path = db.Column(db.String, nullable=False, default='')


def add_ignored_event(src_path, dest_path=''):
    db.session.add(IgnoredEvent(src_path=src_path, dest_path=dest_path))
    db.session.commit()


def pop_ignored_event(src_path=None, dest_path=None):
    """Remove and return True if a matching ignored event exists."""
    query = IgnoredEvent.query
    if src_path is not None:
        query = query.filter_by(src_path=src_path)
    if dest_path is not None:
        query = query.filter_by(dest_path=dest_path)
    event = query.first()
    if event:
        db.session.delete(event)
        db.session.commit()
        return True
    return False


def init_db(app):
    with app.app_context():
        # Ensure foreign keys are enforced when the SQLite connection is opened
        @event.listens_for(db.engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.close()

        # create or migrate database
        if "db" not in sys.argv:
            if not os.path.exists(DB_FILE):
                db.create_all()
                command.stamp(get_alembic_cfg(), "head")
                logger.info("Database created and stamped to the latest migration version.")
            else:
                logger.info('Checking database migration...')
                if is_migration_needed():
                    create_db_backup()
                    upgrade()
                    logger.info("Database migration applied successfully.")

def file_exists_in_db(filepath):
    return Files.query.filter_by(filepath=filepath).first() is not None

def get_file_from_db(file_id):
    return Files.query.filter_by(id=file_id).first()

def create_file(library_id, filepath, file_info):
    """Insert a Files row from an already-fetched file_info dict. Returns the new row."""
    new_file = Files(
        filepath=filepath,
        library_id=library_id,
        folder=file_info["filedir"],
        filename=file_info["filename"],
        extension=file_info["extension"],
        size=file_info["size"],
        mtime=file_info["mtime"],
    )
    db.session.add(new_file)
    db.session.commit()
    return new_file


def update_file_path(library, old_path, new_path):
    try:
        # Find the file entry in the database using the old_path
        file_entry = Files.query.filter_by(filepath=old_path).one()

        # Extract the new folder and filename from the new_path
        folder = os.path.dirname(new_path)
        if os.path.normpath(library) == os.path.normpath(folder):
            # file is at the root of the library
            new_folder = ''
        else:
            new_folder = folder.replace(library, '')
            new_folder = '/' + new_folder if not new_folder.startswith('/') else new_folder

        filename = os.path.basename(new_path)

        # Update the file entry with the new path values
        file_entry.filename = filename
        file_entry.filepath = new_path
        file_entry.folder = new_folder
        
        # Commit the changes to the database
        db.session.commit()

        logger.debug(f"File path updated successfully from {old_path} to {new_path}.")
    
    except NoResultFound:
        logger.warning(f"No file entry found for the path: {old_path}.")
    except IntegrityError:
        db.session.rollback()
        raise
    except Exception as e:
        db.session.rollback()  # Roll back the session in case of an error
        logger.error(f"An error occurred while updating the file path: {str(e)}")

def get_all_apps():
    apps_list = [
        {
            "id": app.id,
            "title_id": app.title.title_id,  # Access the actual title_id from Titles
            "app_id": app.app_id,
            "app_version": app.app_version,
            "app_type": app.app_type,
            "owned": app.owned
        }
        for app in Apps.query.options(db.joinedload(Apps.title)).all()  # Optimized with joinedload
    ]
    return apps_list

def get_all_non_identified_files_from_library(library_id):
    return Files.query.filter_by(
        identified=False, library_id=library_id, identification_attempts=0
    ).all()

def get_files_with_identification_from_library(library_id, identification_type):
    return Files.query.filter_by(library_id=library_id, identification_type=identification_type).all()

def get_filtered_files(content_filter=None) -> list:
    """Get files from database with optional content type filtering."""

    if not content_filter:
        # No filter: get all files regardless of identification status
        query = Files.query
    else:
        query = Files.query.filter_by(identified=True)

        if content_filter == 'multi':
            query = query.filter_by(multicontent=True)
        elif content_filter in APP_TYPE_FILTERS.keys():
            expected_type = APP_TYPE_FILTERS[content_filter]
            query = query.filter_by(multicontent=False).join(Files.apps).filter(Apps.app_type == expected_type)
    
    # Execute query and return files
    return query.all()

def get_shop_files():
    results = Files.query.all()
    shop_files = [{
        "id": file.id,
        "filename": file.filename,
        "size": file.size
    } for file in results]
    return shop_files

def get_libraries():
    return Libraries.query.all()

def add_library(library_path):
    stmt = insert(Libraries).values(path=library_path).on_conflict_do_nothing()
    db.session.execute(stmt)
    db.session.commit()

def get_library(library_id):
    return Libraries.query.filter_by(id=library_id).first()

def get_library_path(library_id):
    library_path = None
    library = Libraries.query.filter_by(id=library_id).first()
    if library:
        library_path = library.path
    return library_path

def get_library_id(library_path):
    library_id = None
    library = Libraries.query.filter_by(path=library_path).first()
    if library:
        library_id = library.id
    return library_id

def get_library_file_paths(library_id):
    return [file.filepath for file in Files.query.filter_by(library_id=library_id).all()]

def set_library_scan_time(library_id, scan_time=None):
    library = get_library(library_id)
    library.last_scan = scan_time or datetime.datetime.now()
    db.session.commit()

def get_all_titles():
    return Titles.query.all()

def get_title(title_id):
    return Titles.query.filter_by(title_id=title_id).first()

def get_title_id_db_id(title_id):
    title = get_title(title_id)
    return title.id

def add_title_id_in_db(title_id):
    existing_title = Titles.query.filter_by(title_id=title_id).first()

    if not existing_title:
        try:
            new_title = Titles(title_id=title_id)
            db.session.add(new_title)
            db.session.commit()
        except Exception:
            # Another worker inserted the same title concurrently
            db.session.rollback()

def get_all_title_apps(title_id):
    title = Titles.query.options(joinedload(Titles.apps)).filter_by(title_id=title_id).first()
    return[to_dict(a)  for a in title.apps]

def get_app_by_id_and_version(app_id, app_version):
    """Get app entry for a specific app_id and version (unique due to constraint)"""
    return Apps.query.filter_by(app_id=app_id, app_version=app_version).first()

def add_file_to_app(app_id, app_version, file_id):
    """Add a file to an existing app using many-to-many relationship"""
    app = get_app_by_id_and_version(app_id, app_version)
    if app:
        file_obj = get_file_from_db(file_id)
        if file_obj and file_obj not in app.files:
            try:
                app.files.append(file_obj)
                app.owned = True
                db.session.commit()
            except Exception:
                # Another worker added the same file association concurrently
                db.session.rollback()
            return True
    return False

def reset_file_identification(file):
    """Clear identification state on a Files row so it will be re-identified."""
    file.identified = False
    file.identification_error = None
    file.identification_type = None
    file.identification_attempts = 0
    file.nb_content = 0
    file.multicontent = False

def remove_file_from_apps(file_id):
    """Remove a file from all apps that reference it and update owned status"""
    apps_updated = 0
    file_obj = get_file_from_db(file_id)
    
    if file_obj:
        # Snapshot the list — iterating file_obj.apps while removing mutates it
        associated_apps = list(file_obj.apps)

        for app in associated_apps:
            # Remove the file from the app's files relationship
            app.files.remove(file_obj)
            
            # Update owned status based on remaining files
            app.owned = len(app.files) > 0
            apps_updated += 1
            
            logger.debug(f"Removed file_id {file_id} from app {app.app_id} v{app.app_version}. Owned: {app.owned}")
        
        if apps_updated > 0:
            db.session.commit()
    
    return apps_updated

def has_owned_apps(title_id):
    """Check if a title has any owned apps"""
    title = get_title(title_id)
    if not title:
        return False
    
    owned_apps = Apps.query.filter_by(title_id=title.id, owned=True).first()
    return owned_apps is not None

def remove_titles_without_owned_apps():
    """Remove titles that have no owned apps."""
    titles_removed = 0
    for title in get_all_titles():
        if not has_owned_apps(title.title_id):
            logger.debug(f"Removing title {title.title_id} - no owned apps remaining")
            db.session.delete(title)
            titles_removed += 1
    if titles_removed:
        db.session.commit()
    return titles_removed

def delete_file_by_filepath(filepath):
    try:
        # Find file with the given filepath
        file_to_delete = Files.query.filter_by(filepath=filepath).one()
        file_id = file_to_delete.id
        
        # Update Apps table before deleting file
        apps_updated = remove_file_from_apps(file_id)
        
        # Delete file
        db.session.delete(file_to_delete)
        
        # Commit the changes
        db.session.commit()
        
        logger.info(f"File '{filepath}' removed from database.")
        if apps_updated > 0:
            logger.info(f"Updated {apps_updated} app entries to remove file reference.")
            
    except NoResultFound:
        logger.info(f"File '{filepath}' not present in database.")
    except Exception as e:
        # If there's an error, rollback the session
        db.session.rollback()
        logger.error(f"An error occurred while removing the file path: {str(e)}")

def remove_missing_files_from_db():
    """Bulk-delete Files rows whose path no longer exists on disk; recompute affected app ownership."""
    missing = [f for f in Files.query.all() if not os.path.exists(f.filepath)]
    if not missing:
        return
    affected_apps = {a for f in missing for a in f.apps}
    for f in missing:
        db.session.delete(f)
    db.session.flush()
    for app in affected_apps:
        app.owned = len(app.files) > 0
    db.session.commit()
    logger.info(f"Removed {len(missing)} missing files from database.")

def increment_download_count(filepath):
    """Increment the download count for a file by filepath"""
    try:
        file_entry = Files.query.filter_by(filepath=filepath).first()
        if file_entry:
            previous = file_entry.download_count
            file_entry.download_count += 1
            db.session.commit()
            logger.debug(f"Download count incremented for '{filepath}': {file_entry.download_count}")
            return True
    except Exception as e:
        db.session.rollback()
        logger.error(f"An error occurred while incrementing download count for '{filepath}': {str(e)}")
        return False

@throttle(60, key_func=lambda filepath, host: (filepath, host))
def increment_download_count_throttled(filepath, host):
    """Throttled wrapper around increment_download_count per (filepath, host) pair."""
    increment_download_count(filepath)
