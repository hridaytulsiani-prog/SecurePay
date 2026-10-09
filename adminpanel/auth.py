"""Redis-backed session tokens for the admin panel.

Mirrors payments.auth's merchant session pattern, but keyed off Django's
auth.User (staff/superuser) instead of MerchantInfo, and stored under a
separate Redis key prefix so admin and merchant sessions never collide.
"""

import secrets

from django.core.cache import cache

TOKEN_TTL_SECONDS = 1800  # 30 minutes; refreshed on every authenticated request
REDIS_PREFIX = "admin_session"


def _redis_key(token):
    return f"{REDIS_PREFIX}:{token}"


def create_admin_session(user):
    """Issue a new opaque session token for a logged-in admin and store it in Redis."""
    token = secrets.token_urlsafe(32)
    cache.set(_redis_key(token), str(user.id), TOKEN_TTL_SECONDS)
    return token


def get_admin_user_id_from_token(auth_token):
    """Look up the admin user id for a session token, sliding its TTL forward on hit."""
    if not auth_token:
        return None
    try:
        user_id = cache.get(_redis_key(auth_token))
        if not user_id:
            return None
        cache.touch(_redis_key(auth_token), TOKEN_TTL_SECONDS)
        return int(user_id)
    except Exception:
        # Redis being unreachable should fail closed (treat as unauthenticated),
        # not crash the request.
        return None


def destroy_admin_session(auth_token):
    """Invalidate a session token immediately (used on logout)."""
    try:
        cache.delete(_redis_key(auth_token))
    except Exception:
        pass


def get_auth_token_from_request(request):
    """Pull the bearer token out of the Authorization header, if present."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1].strip()
    return None
