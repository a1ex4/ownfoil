import logging

from db import db, TitleRequests, Titles


logger = logging.getLogger('main')


def create_title_request(user_id, title_id, title_name=None):
    title_id = (title_id or '').strip().upper()
    title_name = (title_name or '').strip() or None
    if not title_id:
        return False, 'Missing title_id.', None

    if Titles.query.filter_by(title_id=title_id).first() is not None:
        return False, 'Title is already in the library.', None

    existing = TitleRequests.query.filter_by(user_id=user_id, title_id=title_id, status='open').first()
    if existing is not None:
        return True, 'Request already exists.', existing

    req = TitleRequests(user_id=user_id, title_id=title_id, title_name=title_name, status='open')
    db.session.add(req)
    db.session.commit()
    return True, 'Request created.', req


def list_requests(user_id=None, include_all=False, limit=500):
    q = TitleRequests.query
    if not include_all:
        q = q.filter_by(user_id=user_id)
    return q.order_by(TitleRequests.created_at.desc()).limit(limit).all()
