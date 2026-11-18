"""Role-based access control helpers."""
from functools import wraps

from flask import abort
from flask_login import current_user, login_required


def roles_required(*roles):
    """Allow only users whose role is in `roles`; everyone else gets 403."""
    def decorator(fn):
        @wraps(fn)
        @login_required
        def wrapper(*args, **kwargs):
            if current_user.role not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator
