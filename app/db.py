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

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user = db.Column(db.String(100), unique=True)
    password = db.Column(db.String(100))
    role = db.Column(db.String(100))

    def has_role(self, role):
        return role == self.role

def add_to_titles_db(library, file_info):
    filepath = file_info["filepath"]
    filedir = file_info["filedir"].replace(library, '')
    if exists := db.session.query(
        db.session.query(Files).filter_by(filepath=filepath).exists()
    ).scalar():
        return

    print(f'New file to add: {filepath}')
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
    )
    db.session.add(new_title)

    db.session.commit()

def get_all_titles_from_db():
    results = db.session.query(Files.title_id).distinct()
    return [row[0] for row in results.all()]

def get_all_title_files(title_id):
    title_id = title_id.upper()
    results = db.session.query(Files).filter_by(title_id=title_id).all()
    return [to_dict(r) for r in results]