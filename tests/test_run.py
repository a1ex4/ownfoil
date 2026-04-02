"""Verify the production entrypoint (run.py) hooks work correctly."""

import pytest


def test_post_worker_init_disposes_engine(tmp_path, monkeypatch):
    """db.engine.dispose() in post_worker_init must run inside an app context."""
    import db as db_module
    from app import create_app

    db_path = str(tmp_path / 'ownfoil.db')
    db_uri = f'sqlite:///{db_path}'
    monkeypatch.setattr(db_module, 'DB_FILE', db_path)

    flask_app = create_app(db_uri=db_uri)

    # Simulate what post_worker_init does — must not raise
    with flask_app.app_context():
        from db import db
        db.engine.dispose()


def test_cleanup_tasks(tmp_path, monkeypatch):
    """Startup cleanup: remove completed tasks, fail stale running tasks."""
    import db as db_module
    from app import create_app, init_db
    from tasks import Task, cleanup_tasks, enqueue_task

    db_path = str(tmp_path / 'ownfoil.db')
    db_uri = f'sqlite:///{db_path}'
    monkeypatch.setattr(db_module, 'DB_FILE', db_path)

    flask_app = create_app(db_uri=db_uri)
    init_db(flask_app)

    with flask_app.app_context():
        from db import db

        # Create tasks in various states
        t_running, _ = enqueue_task('update_titledb')
        t_running_id = t_running.id
        t_running.status = 'running'
        db.session.commit()

        t_waiting, _ = enqueue_task('scan_library', {'library_path': '/fake'})
        t_waiting_id = t_waiting.id
        t_waiting.status = 'waiting_for_children'
        db.session.commit()

        t_completed, _ = enqueue_task('add_missing_apps')
        t_completed_id = t_completed.id
        t_completed.status = 'completed'
        db.session.commit()

        t_pending, _ = enqueue_task('generate_library')
        t_pending_id = t_pending.id
        # Leave as pending — should NOT be touched

        cleanup_tasks()

        # Running/waiting tasks are marked failed
        t_running = db.session.get(Task, t_running_id)
        t_waiting = db.session.get(Task, t_waiting_id)
        assert t_running.status == 'failed'
        assert t_running.error_message == 'Interrupted by application restart'
        assert t_waiting.status == 'failed'
        assert t_waiting.error_message == 'Interrupted by application restart'

        # Completed tasks are removed
        assert db.session.get(Task, t_completed_id) is None

        # Pending tasks are untouched
        t_pending = db.session.get(Task, t_pending_id)
        assert t_pending.status == 'pending'

        # After cleanup, enqueueing previously blocked tasks works
        t_new, created = enqueue_task('update_titledb')
        assert created is True
