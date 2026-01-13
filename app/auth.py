from flask import Blueprint, render_template, redirect, url_for, request, jsonify
from flask_login import login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from db import *
from flask_login import LoginManager

import logging

# Retrieve main logger
logger = logging.getLogger('main')

def admin_account_created():
    return len(User.query.filter_by(admin_access=True).all())

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
                # Auth disabled, request ok
                return f(*args, **kwargs)

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
        return success, error, is_admin

    username = auth.username
    password = auth.password
    user = User.query.filter_by(user=username).first()
    if user is None:
        success = False
        error = f'Unknown user "{username}".'
    
    elif not check_password_hash(user.password, password):
        success = False
        error = f'Incorrect password for user "{username}".'

    elif not user.has_shop_access():
        success = False
        error = f'User "{username}" does not have access to the shop.'

    else:
        is_admin = user.has_admin_access()
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
    if not user or not check_password_hash(user.password, password):
        logger.warning(f'Incorrect login for user {username}')
        return redirect(url_for('auth.login')) # if the user doesn't exist or password is wrong, reload the page

    # if the above check passes, then we know the user has the right credentials
    logger.info(f'Sucessfull login for user {username}')
    login_user(user, remember=remember)

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
        for db_user in db.session.query(User.id, User.user, User.admin_access, User.shop_access, User.backup_access).all()
    ]
    return jsonify(all_users)

@auth_blueprint.route('/api/user', methods=['DELETE'])
@login_required
@access_required('admin')
def delete_user():
    success = True
    data = request.json
    user_id = data['user_id']
    try:
        User.query.filter_by(id=user_id).delete()
        db.session.commit()
        logger.info(f'Successfully deleted user with id {user_id}.')
    except Exception as e:
        logger.error(f'Could not delete user with id {user_id}: {e}')
        success = False

    resp = {
        'success': success
    } 
    return jsonify(resp)

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
        resp['status_code'] = 302,
        resp['location'] = '/settings'
    
    return jsonify(resp)


@auth_blueprint.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect('/')
