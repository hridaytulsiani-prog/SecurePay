# Merchant-side authentication.
#
# There are TWO independent ways a request can prove it's from a legitimate
# merchant, and this file implements both:
#
#   A) Session token (the normal path): issued at login by
#      merchant.v1.create_merchant.LoginMerchant, stored in Redis, and sent
#      back on every request as either a bare token or "<merchant_id>-<token>"
#      (see _normalize_session_token). This is what get_merchant_id_from_token()
#      checks, and it's what almost every merchant-authenticated view in this
#      project uses (payments/api/v1/generate_order.py, phonepe.py, etc.).
#
#   B) Signed per-order token (a fallback with no session involved): a
#      caller who was never logged in — think a server-to-server webhook or
#      a hosted-checkout redirect link the merchant generated themselves —
#      can instead prove they're that merchant by presenting
#      sha512(merchant_key + merchant_order_id + merchant_salt). Only
#      verify_merchant_auth_token() checks this path; get_merchant_id_from_token()
#      does NOT, so a caller relying on path B must call
#      verify_merchant_auth_token() directly, not the token-lookup shortcut.
#      merchant_key/merchant_salt are the two secrets generated once at
#      signup (see merchant.v1.create_merchant.CreateMerchant) and never
#      change, which is what makes this usable without any prior session.
#
# This is the merchant equivalent of adminpanel/auth.py, which implements
# the identical Redis-session pattern (path A) for Django's own admin/staff
# users instead of MerchantInfo rows. If you're changing the session TTL or
# storage mechanism, check both files — they're independent copies of the
# same idea, not sharing code.
import secrets
from hashlib import sha512

from django.core.cache import cache

from payments.models.merchantinfo import MerchantInfo


TOKEN_TTL_SECONDS = 10 * 60  # 10 minutes; keep in sync with the merchant client's idle timeout.


def _normalize_session_token(merchant_id, auth_token):
    """A session token can arrive in two shapes and this reconciles them
    into one (merchant_id, raw_token) pair:
      - "<merchant_id>-<raw_token>" — the combined form LoginMerchant
        actually issues (see its `combined_token` variable). Self-contained:
        the merchant_id doesn't need to be known/passed in separately.
      - a bare raw_token with `merchant_id` supplied out-of-band (e.g. from
        a URL path parameter or request field alongside the token) — some
        older/alternate callers pass it this way instead.

    If both a combined token's embedded id AND an externally-supplied
    merchant_id are present and they disagree, this returns (None, None)
    (auth failure) rather than picking one — a caller asserting the wrong
    merchant_id for a token that belongs to someone else should never
    silently succeed.
    """
    token = str(auth_token)
    provided_merchant_id = str(merchant_id) if merchant_id is not None else None
    prefix, separator, remainder = token.partition("-")

    if separator and prefix.isdigit() and remainder:
        if provided_merchant_id and prefix != provided_merchant_id:
            return None, None
        return int(prefix), remainder

    if merchant_id is None:
        return None, None

    try:
        return int(merchant_id), token
    except (TypeError, ValueError):
        return None, None


def _verify_session_token_and_refresh(merchant_id, auth_token):
    """Check Redis for `merchant_{id}:{token}`, and — this is the important
    part — reset its TTL back to TOKEN_TTL_SECONDS on every successful check.
    This is what makes the 5-minute TTL behave like an *idle* timeout rather
    than a hard 5-minutes-after-login expiry: as long as the merchant keeps
    making requests at least once every 5 minutes, the session never expires;
    stop for 5+ minutes and the key falls out of Redis on its own (Redis
    handles the actual expiry — nothing here runs a cleanup job)."""
    redis_key = f"merchant_{merchant_id}:{auth_token}"
    try:
        if cache.get(redis_key) is None:
            return False
        cache.touch(redis_key, TOKEN_TTL_SECONDS)
        return True
    except Exception:
        # Redis unreachable -> fail closed (treat as "not authenticated")
        # rather than raising and 500ing every authenticated request.
        return False


def get_merchant_id_from_token(auth_token):
    """The main entry point almost every merchant-authenticated view calls.
    Returns the merchant's id if `auth_token` is a currently-valid Redis
    session token (path A above only — NOT the signed-token fallback), else
    None. Callers should treat None as "reject with 401", not "treat as
    anonymous"."""
    if not auth_token:
        return None

    normalized_merchant_id, normalized_token = _normalize_session_token(None, auth_token)
    if not normalized_merchant_id or not normalized_token:
        return None

    if _verify_session_token_and_refresh(normalized_merchant_id, normalized_token):
        return normalized_merchant_id

    return None


def verify_merchant_auth_token(merchant_id, auth_token, merchant_order_id=None):
    """Like get_merchant_id_from_token, but also accepts the signed
    per-order fallback (path B) when there's no active session — used by
    callers that pass a merchant_order_id and can't rely on a login session
    having happened first (e.g. a hosted-checkout link the merchant
    generates offline and hands to a customer).

    Order of checks:
      1. Is auth_token a valid session token? If so, and merchant_id was
         given, confirm it matches the session's merchant (an active session
         belonging to a DIFFERENT merchant than the one being asserted must
         not pass).
      2. Otherwise, is auth_token the sha512 signature of
         merchant_key + merchant_order_id + merchant_salt for the merchant
         identified by `merchant_id`? merchant_order_id is REQUIRED for this
         path — there's no such thing as a signed token that isn't tied to
         one specific order.
    Returns True/False rather than raising, so callers can use it directly
    in an `if not verify_merchant_auth_token(...): return 401` check.
    """
    if not auth_token:
        return False

    token_merchant_id = get_merchant_id_from_token(auth_token)
    if token_merchant_id is not None:
        if merchant_id is None:
            return True
        try:
            return int(merchant_id) == token_merchant_id
        except (TypeError, ValueError):
            return False

    normalized_merchant_id, normalized_token = _normalize_session_token(merchant_id, auth_token)
    if not normalized_merchant_id or not normalized_token:
        return False

    if not merchant_order_id:
        return False

    try:
        merchant = MerchantInfo.objects.get(id=normalized_merchant_id)
        # secrets.compare_digest (not `==`) deliberately, to avoid leaking
        # timing information about how many leading characters of the token
        # matched — standard practice for comparing secret/signature values.
        expected_auth_token = sha512(
            (merchant.merchant_key + merchant_order_id + merchant.merchant_salt).encode()
        ).hexdigest()
        return secrets.compare_digest(str(auth_token), expected_auth_token)
    except MerchantInfo.DoesNotExist:
        return False
