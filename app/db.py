from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
import json

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

    # print(f'New file to add: {filepath}')
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