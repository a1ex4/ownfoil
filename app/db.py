from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm.exc import NoResultFound
from flask_login import UserMixin
import json, os
import logging

# Retrieve main logger
logger = logging.getLogger('main')

db = SQLAlchemy()


def to_dict(db_results):
    return {c.name: getattr(db_results, c.name) for c in db_results.__table__.columns}

class Files(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filepath = db.Column(db.String)
    library = db.Column(db.String)
    folder = db.Column(db.String)
    filename = db.Column(db.String)
    title_id = db.Column(db.String)
    app_id = db.Column(db.String)
    type = db.Column(db.String)
    version = db.Column(db.String)
    extension = db.Column(db.String)
    size = db.Column(db.Integer)
    identification = db.Column(db.String)

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


def add_to_titles_db(library, file_info):
    filepath = file_info["filepath"]
    filedir = file_info["filedir"].replace(library, '')
    if exists := db.session.query(
        db.session.query(Files).filter_by(filepath=filepath).exists()
    ).scalar():
        existing_entry = db.session.query(Files).filter_by(filepath=filepath).all()
        existing_entry_data = to_dict(existing_entry[0])
        current_identification = existing_entry_data["identification"]
        new_identification = file_info["identification"]
        if new_identification == current_identification:
            return
        else:
            # delete old entry and replace with updated one
            db.session.query(Files).filter_by(filepath=filepath).delete()

    new_title = Files(
        filepath = filepath,
        library = library,
        folder = filedir,
        filename = file_info["filename"],
        title_id = file_info["title_id"],
        app_id = file_info["app_id"],
        type = file_info["type"],
        version = file_info["version"],
        extension = file_info["extension"],
        size = file_info["size"],
        identification = file_info["identification"],
    )
    db.session.add(new_title)

    db.session.commit()

def update_file_path(old_path, new_path):
    try:
        # Find the file entry in the database using the old_path
        file_entry = Files.query.filter_by(filepath=old_path).one()
        
        # Extract the new folder and root_dir from the new_path
        new_folder = "/" + os.path.basename(os.path.dirname(new_path))

        # Update the file entry with the new path values
        file_entry.filepath = new_path
        file_entry.folder = new_folder
        
        # Commit the changes to the database
        db.session.commit()

        logger.info(f"File path updated successfully from {old_path} to {new_path}.")
    
    except NoResultFound:
        logger.warning(f"No file entry found for the path: {old_path}.")
    except Exception as e:
        db.session.rollback()  # Roll back the session in case of an error
        logger.error(f"An error occurred while updating the file path: {str(e)}")

def get_all_titles_from_db():
    # results = db.session.query(Files.title_id).distinct()
    # return [row[0] for row in results]
    results = db.session.query(Files).all()
    return [to_dict(r) for r in results]

def get_all_title_files(title_id):
    title_id = title_id.upper()
    results = db.session.query(Files).filter_by(title_id=title_id).all()
    return [to_dict(r) for r in results]

def get_all_files_with_identification(identification):
    results = db.session.query(Files).filter_by(identification=identification).all()
    return[to_dict(r)['filepath']  for r in results]

def delete_files_by_library(library_path):
    success = True
    errors = []
    try:
        # Find all files with the given library
        files_to_delete = Files.query.filter_by(library=library_path).all()
        
        # Delete each file
        for file in files_to_delete:
            db.session.delete(file)
        
        # Commit the changes
        db.session.commit()
        
        logger.info(f"All entries with library '{library_path}' have been deleted.")
        return success, errors
    except Exception as e:
        # If there's an error, rollback the session
        db.session.rollback()
        logger.error(f"An error occurred: {e}")
        success = False
        errors.append({
            'path': 'library/paths',
            'error': f"An error occurred: {e}"
        })
        return success, errors

def delete_file_by_filepath(filepath):
    try:
        # Find file with the given filepath
        file_to_delete = Files.query.filter_by(filepath=filepath).one()
        
        # Delete file
        db.session.delete(file_to_delete)
        
        # Commit the changes
        db.session.commit()
        
        logger.info(f"File '{filepath}' has been deleted.")
    except Exception as e:
        # If there's an error, rollback the session
        db.session.rollback()
        logger.error(f"An error occurred while deleting the file path: {str(e)}")

def remove_missing_files():
    try:
        # Query all entries in the Files table
        files = Files.query.all()
        
        # List to keep track of IDs to be deleted
        ids_to_delete = []
        
        for file_entry in files:
            # Check if the file exists on disk
            if not os.path.exists(file_entry.filepath):
                # If the file does not exist, mark this entry for deletion
                ids_to_delete.append(file_entry.id)
                logger.debug(f"File not found, marking file for deletion: {file_entry.filepath}")
        
        # Delete all marked entries from the database
        if ids_to_delete:
            Files.query.filter(Files.id.in_(ids_to_delete)).delete(synchronize_session=False)
            db.session.commit()
            logger.info(f"Deleted {len(ids_to_delete)} files from the database.")
        else:
            logger.debug("No files were deleted. All files are present on disk.")
    
    except Exception as e:
        db.session.rollback()  # Rollback in case of an error
        logger.error(f"An error occurred while removing missing files: {str(e)}")