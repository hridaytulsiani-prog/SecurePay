from django.contrib.auth import get_user_model
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.exceptions import PermissionDenied
from rest_framework.views import APIView

from adminpanel.access_control import normalize_special_access, SPECIAL_ACCESS_DEFINITIONS
from adminpanel.auth import get_admin_user_id_from_token, get_auth_token_from_request
from adminpanel.models import AdminUserRole

User = get_user_model()

ROLE_OWNER = AdminUserRole.ROLE_OWNER
ROLE_OPERATIONS = AdminUserRole.ROLE_OPERATIONS
ROLE_AUDITOR = AdminUserRole.ROLE_AUDITOR

ROLE_LABELS = {
    ROLE_OWNER: "Owner",
    ROLE_OPERATIONS: "Operations Manager",
    ROLE_AUDITOR: "Auditor / Viewer",
}

ROLE_PERMISSIONS = {
    ROLE_OWNER: {"*"},
    ROLE_OPERATIONS: {
        "dashboard.view",
        "orders.view",
        "needs_attention.view",
        "enquiries.view",
        "enquiries.manage",
        "decision_history.view",
        "suspicious_pdfs.view",
        "courier.check",
        "courier.decide",
        "aggregator.view",
    },
    ROLE_AUDITOR: {
        "dashboard.view",
        "orders.view",
        "needs_attention.view",
        "enquiries.view",
        "decision_history.view",
        "audit.view",
        "suspicious_pdfs.view",
        "aggregator.view",
    },
}


def get_admin_role(user):
    if user.is_superuser:
        return ROLE_OWNER
    try:
        return user.admin_role.role
    except AdminUserRole.DoesNotExist:
        return ROLE_OPERATIONS if user.is_staff else ROLE_AUDITOR


def role_has_permission(role, permission):
    permissions = ROLE_PERMISSIONS.get(role, set())
    return "*" in permissions or permission in permissions


def user_has_permission(user, permission):
    if role_has_permission(get_admin_role(user), permission):
        return True
    if permission == "users.manage":
        return False
    try:
        rules = normalize_special_access(user.admin_access_policy.access_rules)
    except Exception:
        rules = {}
    for item in SPECIAL_ACCESS_DEFINITIONS:
        if item.get("permission") == permission and rules.get(item["key"], {}).get("enabled"):
            return True
    return False


class AdminAPIView(APIView):
    """Base view requiring a valid admin (staff/superuser) session token.

    Subclasses just implement get/post/patch/etc. as normal; `initial()` runs
    before the handler on every request and stashes the resolved user on
    `request.admin_user` for handlers to use (e.g. to stamp "resolved_by").
    Raising AuthenticationFailed here lets DRF turn it into a clean 401/403
    response automatically, without each view needing its own auth checks.
    """

    required_permission = None

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        token = get_auth_token_from_request(request)
        user_id = get_admin_user_id_from_token(token)
        if not user_id:
            raise AuthenticationFailed("Invalid or expired admin session")

        try:
            user = User.objects.get(id=user_id, is_active=True)
        except User.DoesNotExist:
            raise AuthenticationFailed("Invalid or expired admin session")

        # Belt-and-suspenders: even if a stale session token somehow points at
        # a user who lost staff/superuser status, don't let them in.
        if not (user.is_staff or user.is_superuser):
            raise AuthenticationFailed("Invalid or expired admin session")

        request.admin_user = user
        request.admin_role = get_admin_role(user)

        if self.required_permission and not user_has_permission(user, self.required_permission):
            raise PermissionDenied("You do not have permission to perform this action.")
