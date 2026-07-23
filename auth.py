"""Shared-password gate. Inactive (no login required) when APP_PASSWORD is unset.

Enforcement lives in app.py's before_request hook, which applies this
uniformly to every route rather than requiring a per-view decorator.
"""
import hmac
import os

from flask import session

APP_PASSWORD = os.environ.get("APP_PASSWORD")


def auth_enabled() -> bool:
    return bool(APP_PASSWORD)


def is_authenticated() -> bool:
    if not auth_enabled():
        return True
    return session.get("authenticated") is True


def check_password(password: str) -> bool:
    return APP_PASSWORD is not None and hmac.compare_digest(password, APP_PASSWORD)
