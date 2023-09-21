from flask import Blueprint, render_template, redirect, url_for, request, jsonify
from flask_login import login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from db import *
from flask_login import LoginManager

def role_required(role: str):
    def _role_required(f):
        def decorated_view(*args, **kwargs):
            if not current_user.is_authenticated:
                if len(User.query.filter_by(role='admin').all()):
                    return login_manager.unauthorized()
                else:
                    return f(*args, **kwargs)
            if not current_user.has_role(role):
                return 'Forbidden', 403
            return f(*args, **kwargs)
        return decorated_view
    return _role_required


def roles_required(roles: list, require_all=False):
    def _roles_required(f):
        def decorated_view(*args, **kwargs):
            if len(roles) == 0:
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
    remember = True if request.form.get('remember') else False

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
def profile():
    return render_template('profile.html')

@auth_blueprint.route('/api/users')
@role_required('admin')
def get_users():
    all_users = []
    for db_user in db.session.query(User.id, User.user, User.role).all():
        all_users.append(dict(db_user._mapping))

    return jsonify(all_users)

@auth_blueprint.route('/api/user', methods=['DELETE'])
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
def signup_post():
    signup_success = True
    data = request.json

    username = data['user']
    password = data['password']
    role = data['role']
    print(username, password, role)

    user = User.query.filter_by(user=username).first() # if this returns a user, then the email already exists in database
    
    if user: # if a user is found, we want to redirect back to signup page so user can try again
        print('user already exists')
        return redirect(url_for('auth.signup'))

    # create a new user with the form data. Hash the password so the plaintext version isn't saved.
    new_user = User(user=username, password=generate_password_hash(password, method='scrypt'), role=role)

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