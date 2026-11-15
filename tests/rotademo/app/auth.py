"""Who is signed in, what they may do, and which organisation's data they see.

DEMO-GRADE SIGN-IN: the sign-in page lists the seeded users and signing in is one click, with no
password. It exists to show roles and tenancy, not to be secure. The session stores the user's id,
role and organisation.

Every route that shows or changes tenant data is decorated with @signed_in or @manager_only and
reads data only through scope(), which is bound to the signed-in user's organisation.
"""
from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from flask import abort, current_app, g, jsonify, redirect, request, session, url_for

import data

MANAGER, WORKER = "manager", "worker"


def sign_in(user: dict[str, Any]) -> None:
    session.clear()
    session.update(user_id=user["id"], role=user["role"], org_id=user["org_id"])


def sign_out() -> None:
    session.clear()


def current_user() -> dict[str, Any] | None:
    if "user" not in g:
        user_id = session.get("user_id")
        user = data.user(current_app.db(), int(user_id)) if user_id is not None else None
        # the session must still match the user record (role and organisation)
        if user is not None and (user["role"] != session.get("role") or user["org_id"] != session.get("org_id")):
            user = None
        g.user = user
    return g.user


def scope() -> data.Scope:
    """Data access bound to the signed-in user's organisation."""
    user = current_user()
    if user is None:
        abort(401)
    return data.Scope(current_app.db(), int(user["org_id"]))


def _wants_json() -> bool:
    return request.path.startswith("/api/")


def signed_in(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if current_user() is None:
            if _wants_json():
                return jsonify(error="Sign in first"), 401
            return redirect(url_for("signin"))
        return view(*args, **kwargs)
    return wrapper


def manager_only(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    @signed_in
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if current_user()["role"] != MANAGER:
            if _wants_json():
                return jsonify(error="Only ward managers can do this"), 403
            abort(403)
        return view(*args, **kwargs)
    return wrapper
