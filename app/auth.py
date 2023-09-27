from flask import Blueprint, render_template, redirect, url_for, request, jsonify
from flask_login import login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from db import *
from flask_login import LoginManager

def access_required(access: str):
    def _access_required(f):
        @wraps(f)
        def decorated_view(*args, **kwargs):
            if not current_user.is_authenticated:
                if len(User.query.filter_by(admin_access=True).all()):
                    return login_manager.unauthorized()
                else:
                    return f(*args, **kwargs)
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

auth_blueprint = Blueprint('auth', __name__)

login_manager = LoginManager()
login_manager.login_view = 'auth.login'

@auth_blueprint.route('/login')
def login():
    return render_template('login.html')

@auth_blueprint.route('/login', methods=['POST'])
def login_post():
    # login code goes here
    username = request.form.get('user')
    password = request.form.get('password')
    remember = bool(request.form.get('remember'))

    user = User.query.filter_by(user=username).first()

    # check if the user actually exists
    # take the user-supplied password, hash it, and compare it to the hashed password in the database
    if not user or not check_password_hash(user.password, password):
        print('incorrect login')
        return redirect(url_for('auth.login')) # if the user doesn't exist or password is wrong, reload the page

    # if the above check passes, then we know the user has the right credentials
    print('correct login')
    login_user(user, remember=remember)
    return redirect(url_for('auth.profile'))

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
    try:
        data = request.json
        user_id = data['user_id']
        User.query.filter_by(id=user_id).delete()
        db.session.commit()
    except Exception as e:
        print(e)
        success = False

    resp = {
        'success': success
    } 
    return jsonify(resp)

@auth_blueprint.route('/api/user/signup', methods=['POST'])
@access_required('admin')
def signup_post():
    signup_success = True
    data = request.json

    username = data['user']
    password = data['password']
    shop_access = data['shop_access']
    backup_access = data['backup_access']
    admin_access = data['admin_access']

    user = User.query.filter_by(user=username).first() # if this returns a user, then the email already exists in database
    
    if user: # if a user is found, we want to redirect back to signup page so user can try again
        print('user already exists')
        # Todo redirect to incoming page or return success: false
        return redirect(url_for('auth.signup'))

    # create a new user with the form data. Hash the password so the plaintext version isn't saved.
    new_user = User(user=username, password=generate_password_hash(password, method='scrypt'), admin_access=admin_access, shop_access=shop_access, backup_access=backup_access)

    # add the new user to the database
    db.session.add(new_user)
    db.session.commit()

    resp = {
        'success': signup_success
    } 
    return jsonify(resp)


@auth_blueprint.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect('/')