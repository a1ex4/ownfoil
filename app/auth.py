from flask import Blueprint, render_template, redirect, url_for, request, jsonify
from flask_login import login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from db import *
from flask_login import LoginManager
from settings import load_settings, set_security_settings

import logging
import threading
import time

# Retrieve main logger
logger = logging.getLogger('main')

_recent_auth_log_lock = threading.Lock()
_recent_auth_log = {}


def _auth_dedupe_allow(dedupe_key: str, window_s: int = 15) -> bool:
    now = time.time()
    key = str(dedupe_key or '')[:512]
    with _recent_auth_log_lock:
        last = _recent_auth_log.get(key) or 0
        if now - last < float(window_s):
            return False
        _recent_auth_log[key] = now
        if len(_recent_auth_log) > 5000:
            ordered = sorted(_recent_auth_log.items(), key=lambda kv: kv[1], reverse=True)
            _recent_auth_log.clear()
            for k, ts in ordered[:2000]:
                _recent_auth_log[k] = ts
    return True


def _log_login_event(kind: str, username: str = None, ok: bool = None, status_code: int = None, window_s: int = 10):
    try:
        settings = {}
        try:
            settings = load_settings()
        except Exception:
            settings = {}
        remote = _effective_client_ip(settings)
        ua = request.headers.get('User-Agent')

        # Dedupe noisy auth failures (scanners, repeated retries).
        dedupe_key = f"{kind}|{(username or '').strip()}|{remote}|{ua}"[:512]
        if window_s and not _auth_dedupe_allow(dedupe_key, window_s=window_s):
            return

        add_access_event(
            kind=kind,
            user=(username or '').strip() or None,
            remote_addr=remote,
            user_agent=ua,
            ok=bool(ok) if ok is not None else None,
            status_code=(
                int(status_code)
                if status_code is not None
                else (200 if ok else 401)
            ),
        )
    except Exception:
        # Avoid breaking auth flow on logging failures.
        try:
            logger.exception('Failed to log login event')
        except Exception:
            pass

def admin_account_created():
    # Setup mode is active until at least one admin user exists.
    # If setup was explicitly completed, do not fall back into setup mode.
    try:
        settings = load_settings()
        if _setup_complete(settings):
            return True
    except Exception:
        pass

    try:
        return User.query.filter_by(admin_access=True).count() > 0
    except Exception:
        return False


def _setup_complete(settings: dict) -> bool:
    try:
        return bool((settings or {}).get('security', {}).get('setup_complete', False))
    except Exception:
        return False


def _bootstrap_private_only(settings: dict) -> bool:
    try:
        return bool((settings or {}).get('security', {}).get('bootstrap_private_networks_only', True))
    except Exception:
        return True


def _trusted_proxies(settings: dict):
    try:
        return list((settings or {}).get('security', {}).get('trusted_proxies') or [])
    except Exception:
        return []


def _trust_proxy_headers(settings: dict) -> bool:
    try:
        return bool((settings or {}).get('security', {}).get('trust_proxy_headers', False))
    except Exception:
        return False


def _peer_ip() -> str:
    return (request.remote_addr or '').strip()


def _parse_first_ip_list(value: str) -> str:
    if not value:
        return ''
    # X-Forwarded-For can be: "client, proxy1, proxy2"
    return value.split(',', 1)[0].strip()


def _peer_is_trusted_proxy(settings: dict) -> bool:
    try:
        import ipaddress
        peer = _peer_ip()
        if not peer:
            return False
        peer_ip = ipaddress.ip_address(peer)
        entries = _trusted_proxies(settings)
        if not entries:
            return False
        for entry in entries:
            entry = str(entry).strip()
            if not entry:
                continue
            try:
                if '/' in entry:
                    if peer_ip in ipaddress.ip_network(entry, strict=False):
                        return True
                else:
                    if peer_ip == ipaddress.ip_address(entry):
                        return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def _effective_client_ip(settings: dict) -> str:
    """Return client IP, trusting XFF only from configured proxies."""
    peer = _peer_ip()
    xff = (request.headers.get('X-Forwarded-For') or '').strip()
    if xff and _trust_proxy_headers(settings) and _peer_is_trusted_proxy(settings):
        candidate = _parse_first_ip_list(xff)
        if candidate:
            return candidate
    return peer


def _is_private_ip(value: str) -> bool:
    try:
        import ipaddress
        if not value:
            return False
        ip = ipaddress.ip_address(value)
        return bool(ip.is_private or ip.is_loopback)
    except Exception:
        return False


def _bootstrap_request_allowed(settings: dict) -> bool:
    """Bootstrap is only allowed from private networks.

    If a reverse proxy is used (X-Forwarded-For present), require explicit proxy trust config.
    """
    peer = _peer_ip()
    xff = (request.headers.get('X-Forwarded-For') or '').strip()

    if xff:
        # Don't trust XFF unless explicitly configured and peer is trusted.
        if not _trust_proxy_headers(settings) or not _peer_is_trusted_proxy(settings):
            return False
        client = _effective_client_ip(settings)
        return _is_private_ip(client)

    # Direct connection.
    return _is_private_ip(peer)


def _render_setup_required(reason: str = ''):
    peer = _peer_ip()
    xff = (request.headers.get('X-Forwarded-For') or '').strip()
    return render_template(
        'setup_required.html',
        title='Setup required',
        reason=reason,
        peer_addr=peer,
        x_forwarded_for=xff,
    ), 403

def unauthorized_json():
    response = login_manager.unauthorized()
    resp = {
        'success': False,
        'status_code': response.status_code,
        'location': response.location
    }
    return jsonify(resp)

def access_required(access: str):
    def _access_required(f):
        @wraps(f)
        def decorated_view(*args, **kwargs):
            if not admin_account_created():
                # Setup mode: do NOT disable auth globally.
                # Optionally allow bootstrap only from private networks.
                _app_settings = {}
                try:
                    _app_settings = load_settings()
                except Exception:
                    _app_settings = {}

                if _setup_complete(_app_settings):
                    # Safety latch: don't ever re-enter setup mode automatically.
                    return 'Forbidden', 403

                setup_allow = (access == 'admin' and request.path in ('/users', '/api/user/signup'))
                if _bootstrap_private_only(_app_settings) and not _bootstrap_request_allowed(_app_settings):
                    # Show a friendly page to non-private clients during setup.
                    if request.path.startswith('/api/'):
                        return jsonify({'success': False, 'error': 'Forbidden'}), 403
                    reason = 'Access denied from this network during initial setup.'
                    if request.headers.get('X-Forwarded-For'):
                        reason = reason + ' Reverse proxy detected; configure security.trusted_proxies and enable security.trust_proxy_headers.'
                    return _render_setup_required(reason)

                if setup_allow:
                    return f(*args, **kwargs)

                if request.path.startswith('/api/'):
                    return jsonify({'success': False, 'error': 'Setup required: create the first admin user at /users.'}), 403
                return redirect('/users')

            if not current_user.is_authenticated:
                # return unauthorized_json()
                return login_manager.unauthorized()

            if not current_user.has_access(access):
                return 'Forbidden', 403
            return f(*args, **kwargs)
        return decorated_view
    return _access_required


def roles_required(roles: list, require_all=False):
    def _roles_required(f):
        @wraps(f)
        def decorated_view(*args, **kwargs):
            if not roles:
                raise ValueError('Empty list used when requiring a role.')
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if require_all and not all(current_user.has_role(role) for role in roles):
                return 'Forbidden', 403
            elif not require_all and not any(current_user.has_role(role) for role in roles):
                return 'Forbidden', 403
            return f(*args, **kwargs)

        return decorated_view

    return _roles_required

def basic_auth(request):
    success = True
    error = ''
    is_admin = False

    auth = request.authorization
    if auth is None:
        success = False
        error = 'Shop requires authentication.'
        _log_login_event('shop_auth_missing', username=None, ok=False, status_code=401, window_s=30)
        return success, error, is_admin

    username = auth.username
    password = auth.password
    user = User.query.filter_by(user=username).first()
    if user is None:
        success = False
        error = f'Unknown user "{username}".'
        _log_login_event('shop_auth_failed_unknown_user', username=username, ok=False, status_code=401, window_s=30)
    
    elif not check_password_hash(user.password, password):
        success = False
        error = f'Incorrect password for user "{username}".'
        _log_login_event('shop_auth_failed_bad_password', username=username, ok=False, status_code=401, window_s=30)

    elif getattr(user, 'frozen', False):
        success = False
        message = (getattr(user, 'frozen_message', None) or '').strip()
        error = message if message else 'Account is frozen.'
        _log_login_event('shop_auth_denied_frozen', username=username, ok=False, status_code=403, window_s=60)

    elif not user.has_shop_access():
        success = False
        error = f'User "{username}" does not have access to the shop.'
        _log_login_event('shop_auth_denied_no_access', username=username, ok=False, status_code=403, window_s=60)

    else:
        is_admin = user.has_admin_access()
        # Basic auth may be sent on every request; dedupe to avoid log spam.
        _log_login_event('shop_auth_success', username=username, ok=True, status_code=200, window_s=60)
    return success, error, is_admin

auth_blueprint = Blueprint('auth', __name__)

login_manager = LoginManager()
login_manager.login_view = 'auth.login'

def create_or_update_user(username, password, admin_access=False, shop_access=False, backup_access=False):
    """
    Create a new user or update an existing user with the given credentials and access rights.
    """
    user = User.query.filter_by(user=username).first()
    if user:
        logger.info(f'Updating existing user {username}')
        user.admin_access = admin_access
        user.shop_access = shop_access
        user.backup_access = backup_access
        if getattr(user, 'frozen', False) and admin_access:
            user.frozen = False
            user.frozen_message = None
        user.password = generate_password_hash(password, method='scrypt')
    else:
        logger.info(f'Creating new user {username}')
        new_user = User(user=username, password=generate_password_hash(password, method='scrypt'), admin_access=admin_access, shop_access=shop_access, backup_access=backup_access)
        db.session.add(new_user)
    db.session.commit()

def init_user_from_environment(environment_name, admin=False):
    """
    allow to init some user from environment variable to init some users without using the UI
    """
    username = os.getenv(environment_name + '_NAME')
    password = os.getenv(environment_name + '_PASSWORD')
    if username and password:
        if admin:
            logger.info('Initializing an admin user from environment variable...')
            admin_access = True
            shop_access = True
            backup_access = True
        else:
            logger.info('Initializing a regular user from environment variable...')
            admin_access = False
            shop_access = True
            backup_access = False

        if not admin:
            existing_admin = admin_account_created()
            if not existing_admin and not admin_access:
                logger.error(f'Error creating user {username}, first account created must be admin')
                return

        create_or_update_user(username, password, admin_access, shop_access, backup_access)

def init_users(app):
    with app.app_context():
        # init users from ENV
        if os.environ.get('USER_ADMIN_NAME') is not None:
            init_user_from_environment(environment_name="USER_ADMIN", admin=True)
        if os.environ.get('USER_GUEST_NAME') is not None:
            init_user_from_environment(environment_name="USER_GUEST", admin=False)

@auth_blueprint.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        next_url = request.args.get('next', '')
        if current_user.is_authenticated:
            return redirect(next_url if len(next_url) else '/')
        return render_template('login.html', title='Login')
        
    # login code goes here
    username = request.form.get('user')
    password = request.form.get('password')
    remember = bool(request.form.get('remember'))
    next_url = request.form.get('next', '')

    user = User.query.filter_by(user=username).first()

    # check if the user actually exists
    # take the user-supplied password, hash it, and compare it to the hashed password in the database
    if not user:
        logger.warning(f'Incorrect login for user {username}')
        _log_login_event('login_failed_unknown_user', username=username, ok=False, status_code=401, window_s=15)
        return redirect(url_for('auth.login')) # if the user doesn't exist or password is wrong, reload the page

    # take the user-supplied password, hash it, and compare it to the hashed password in the database
    if not check_password_hash(user.password, password):
        logger.warning(f'Incorrect login for user {username}')
        _log_login_event('login_failed_bad_password', username=username, ok=False, status_code=401, window_s=15)
        return redirect(url_for('auth.login'))

    if getattr(user, 'frozen', False):
        logger.warning(f'Blocked login for frozen user {username}')
        _log_login_event('login_denied_frozen', username=username, ok=False, status_code=403, window_s=30)
        return redirect(url_for('auth.login'))

    # if the above check passes, then we know the user has the right credentials
    logger.info(f'Sucessfull login for user {username}')
    login_user(user, remember=remember)
    _log_login_event('login_success', username=username, ok=True, status_code=200, window_s=0)

    return redirect(next_url if len(next_url) else '/')

@auth_blueprint.route('/profile')
@login_required
@access_required('backup')
def profile():
    return render_template('profile.html')

@auth_blueprint.route('/api/users')
@access_required('admin')
def get_users():
    all_users = [
        dict(db_user._mapping)
        for db_user in db.session.query(
            User.id,
            User.user,
            User.admin_access,
            User.shop_access,
            User.backup_access,
            User.frozen,
            User.frozen_message,
        ).all()
    ]
    return jsonify(all_users)


@auth_blueprint.route('/api/user/freeze', methods=['PATCH'])
@login_required
@access_required('admin')
def freeze_user():
    success = True
    errors = []
    data = request.json or {}
    user_id = data.get('user_id')
    frozen = data.get('frozen')
    message = (data.get('message') or '').strip()

    if not user_id:
        errors.append('Missing user id.')
    if frozen is None:
        errors.append('Missing frozen state.')

    user = User.query.filter_by(id=user_id).first() if not errors else None
    if not user:
        errors.append('User not found.')

    if errors:
        success = False
    else:
        user.frozen = bool(frozen)
        user.frozen_message = message if user.frozen else None
        db.session.commit()
        logger.info(f"Updated frozen state for user {user.id} ({user.user}): {user.frozen}")

    return jsonify({'success': success, 'errors': errors})

@auth_blueprint.route('/api/user', methods=['DELETE'])
@login_required
@access_required('admin')
def delete_user():
    data = request.json or {}
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Missing user_id.'}), 400

    user = User.query.filter_by(id=user_id).first()
    if not user:
        return jsonify({'success': False, 'error': 'User not found.'}), 404

    # Prevent accidentally removing the last admin account.
    if bool(getattr(user, 'admin_access', False)):
        admin_count = User.query.filter_by(admin_access=True).count()
        if admin_count <= 1:
            return jsonify({'success': False, 'error': 'Cannot delete the last admin account.'}), 400

    try:
        User.query.filter_by(id=user_id).delete()
        db.session.commit()
        logger.info(f'Successfully deleted user with id {user_id}.')
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        logger.error(f'Could not delete user with id {user_id}: {e}')
        return jsonify({'success': False, 'error': 'Delete failed.'}), 500

@auth_blueprint.route('/api/user', methods=['PATCH'])
@login_required
@access_required('admin')
def update_user():
    success = True
    errors = []
    data = request.json or {}
    user_id = data.get('user_id')
    username = (data.get('user') or '').strip()
    password = data.get('password')
    admin_access = data.get('admin_access')
    shop_access = data.get('shop_access')
    backup_access = data.get('backup_access')

    if not user_id:
        errors.append('Missing user id.')
    if not username:
        errors.append('Username is required.')
    if admin_access is None or shop_access is None or backup_access is None:
        errors.append('Missing access configuration.')

    user = User.query.filter_by(id=user_id).first() if not errors else None
    if not user:
        errors.append('User not found.')

    if user and username != user.user:
        existing_user = User.query.filter_by(user=username).first()
        if existing_user:
            errors.append('Username already exists.')

    if user and user.admin_access and admin_access is False:
        admin_count = User.query.filter_by(admin_access=True).count()
        if admin_count <= 1:
            errors.append('Cannot remove the last admin account.')

    if errors:
        success = False
    else:
        if admin_access:
            shop_access = True
            backup_access = True
        user.user = username
        user.admin_access = admin_access
        user.shop_access = shop_access
        user.backup_access = backup_access
        if getattr(user, 'frozen', False) and admin_access:
            user.frozen = False
            user.frozen_message = None
        if password:
            user.password = generate_password_hash(password, method='scrypt')
        db.session.commit()
        logger.info(f'Successfully updated user {user.id} ({username}).')

    resp = {
        'success': success,
        'errors': errors
    }
    return jsonify(resp)

@auth_blueprint.route('/api/user/password', methods=['PATCH'])
@login_required
@access_required('admin')
def reset_user_password():
    success = True
    errors = []
    data = request.json or {}
    user_id = data.get('user_id')
    password = data.get('password')

    if not user_id:
        errors.append('Missing user id.')
    if not password:
        errors.append('Password is required.')

    user = User.query.filter_by(id=user_id).first() if not errors else None
    if not user:
        errors.append('User not found.')

    if errors:
        success = False
    else:
        user.password = generate_password_hash(password, method='scrypt')
        db.session.commit()
        logger.info(f'Successfully reset password for user {user.id} ({user.user}).')

    resp = {
        'success': success,
        'errors': errors
    }
    return jsonify(resp)

@auth_blueprint.route('/api/user/signup', methods=['POST'])
@access_required('admin')
def signup_post():
    signup_success = True
    data = request.json

    username = data['user']
    password = data['password']
    admin_access = data['admin_access']
    if admin_access:
        shop_access = True
        backup_access = True
    else:
        shop_access = data['shop_access']
        backup_access = data['backup_access']

    user = User.query.filter_by(user=username).first() # if this returns a user, then the user already exists in database
    
    if user: # if a user is found, we want to redirect back to signup page so user can try again
        logger.error(f'Error creating user {username}, user already exists')
        # Todo redirect to incoming page or return success: false
        return redirect(url_for('auth.signup'))
    
    existing_admin = admin_account_created()
    if not existing_admin and not admin_access:
        logger.error(f'Error creating user {username}, first account created must be admin')
        resp = {
            'success': False,
            'status_code': 400,
            'location': '/settings',
        } 
        return jsonify(resp)

    # create a new user with the form data. Hash the password so the plaintext version isn't saved.
    create_or_update_user(username, password, admin_access, shop_access, backup_access)
    
    logger.info(f'Successfully created user {username}.')

    resp = {
        'success': signup_success
    } 

    if not existing_admin and admin_access:
        logger.debug('First admin account created')
        try:
            set_security_settings({'setup_complete': True})
        except Exception:
            pass
        resp['status_code'] = 302,
        resp['location'] = '/settings'
    
    return jsonify(resp)


@auth_blueprint.route('/logout')
@login_required
def logout():
    try:
        username = None
        try:
            if current_user.is_authenticated:
                username = current_user.user
        except Exception:
            username = None
        _log_login_event('logout', username=username, ok=True, status_code=200, window_s=0)
    except Exception:
        pass
    logout_user()
    return redirect('/')
