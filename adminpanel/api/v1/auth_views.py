# Admin panel login/logout/session-check endpoints.
#
# --- How to create the FIRST admin user (there's no signup form) ----------
# Unlike merchant accounts (merchant.v1.create_merchant.CreateMerchant),
# there is NO API endpoint to create an admin user — that's deliberate,
# since anyone who could self-register as an admin would defeat the point of
# having an admin panel. Instead, admin accounts are plain Django
# auth.User rows, created with Django's own tooling:
#
#     python manage.py createsuperuser
#
# (superuser implies staff, so this is enough on its own). Or, to promote an
# EXISTING regular Django user to be able to log into this admin panel
# without making them a full superuser:
#
#     python manage.py shell -c "
#     from django.contrib.auth import get_user_model
#     u = get_user_model().objects.get(username='someone')
#     u.is_staff = True
#     u.save()
#     "
#
# Either way, that same username/password is what gets POSTed to
# AdminLoginView below — this reuses Django's built-in User model and
# password hashing (authenticate()) rather than inventing a separate admin
# credential store.
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from rest_framework.views import APIView, Response, status

from adminpanel.auth import (
    TOKEN_TTL_SECONDS,
    create_admin_session,
    destroy_admin_session,
    get_auth_token_from_request,
)
from adminpanel.access_control import normalize_special_access, serialize_special_access
from adminpanel.permissions import AdminAPIView
from adminpanel.models import AdminUserAccessPolicy, AdminUserRole
from adminpanel.permissions import get_admin_role, role_has_permission, ROLE_LABELS

User = get_user_model()
DEFAULT_ADMIN_SETUP_KEY = "securepay-admin-setup"


def _serialize_admin_user(user):
    role = get_admin_role(user)
    try:
        access_rules = user.admin_access_policy.access_rules
    except AdminUserAccessPolicy.DoesNotExist:
        access_rules = {}
    special_access = serialize_special_access(access_rules)
    for item in special_access:
        inherited = role_has_permission(role, item["permission"])
        item["inherited"] = inherited
        if inherited:
            item["enabled"] = True

    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "is_active": user.is_active,
        "role": role,
        "role_label": ROLE_LABELS.get(role, role),
        "created_at": user.date_joined,
        "special_access": special_access,
    }


def _owner_count():
    superuser_ids = set(User.objects.filter(is_superuser=True, is_active=True).values_list("id", flat=True))
    explicit_owner_ids = set(
        AdminUserRole.objects.filter(role=AdminUserRole.ROLE_OWNER, user__is_active=True).values_list("user_id", flat=True)
    )
    return len(superuser_ids | explicit_owner_ids)


class AdminLoginView(APIView):
    """Authenticate against Django's normal auth.User (staff/superuser only).

    No AdminAPIView base here since there's no session yet to check — this is
    the endpoint that creates one.
    """

    def post(self, request):
        username = (request.data.get("username") or "").strip()
        password = request.data.get("password")

        if not all([username, password]):
            return Response(
                {"error": "username and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        auth_username = username
        if "@" in username:
            email_user = User.objects.filter(email__iexact=username).first()
            if email_user:
                auth_username = email_user.username

        user = authenticate(request, username=auth_username, password=password)
        # Reject non-staff users even if the password is correct — this login
        # is for the admin panel only, not general site auth.
        if user is None or not (user.is_staff or user.is_superuser):
            return Response({"error": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED)

        token = create_admin_session(user)

        return Response(
            {
                "message": "Login successful.",
                "token": token,
                "expires_in": TOKEN_TTL_SECONDS,
                "admin": _serialize_admin_user(user),
            },
            status=status.HTTP_200_OK,
        )


class AdminLogoutView(AdminAPIView):
    """Drop the current session token so it can no longer be used."""

    def post(self, request):
        token = get_auth_token_from_request(request)
        destroy_admin_session(token)
        return Response({"message": "Logged out."}, status=status.HTTP_200_OK)


class AdminMeView(AdminAPIView):
    """Used by the frontend on load to check whether a stored token is still valid
    and to populate the logged-in admin's name in the UI."""

    def get(self, request):
        return Response(_serialize_admin_user(request.admin_user))


class AdminAccountCreateView(APIView):
    """Create a Django staff/superuser account from a hidden setup UI route.

    If no superuser exists yet, this can create the first top-level account
    without login. After setup, a server-side ADMIN_ACCOUNT_SETUP_KEY is
    required so this does not become open admin self-registration.
    """

    def post(self, request):
        has_superuser = User.objects.filter(is_superuser=True).exists()
        setup_key = (request.data.get("setup_key") or "").strip()
        configured_setup_key = getattr(settings, "ADMIN_ACCOUNT_SETUP_KEY", "")
        valid_setup_keys = {DEFAULT_ADMIN_SETUP_KEY}
        if configured_setup_key:
            valid_setup_keys.add(configured_setup_key)

        if has_superuser and not valid_setup_keys:
            return Response(
                {"error": "Admin setup is locked. Configure ADMIN_ACCOUNT_SETUP_KEY on the backend to create more admin accounts from this page."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if has_superuser and setup_key not in valid_setup_keys:
            return Response({"error": "Invalid setup key."}, status=status.HTTP_403_FORBIDDEN)

        username = (request.data.get("username") or "").strip()
        email = (request.data.get("email") or "").strip()
        password = request.data.get("password") or ""
        confirm_password = request.data.get("confirm_password") or ""
        role = (request.data.get("role") or AdminUserRole.ROLE_OPERATIONS).strip()

        if not username or not password:
            return Response(
                {"error": "username and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if password != confirm_password:
            return Response({"error": "passwords do not match."}, status=status.HTTP_400_BAD_REQUEST)
        if len(password) < 8:
            return Response(
                {"error": "password must be at least 8 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if User.objects.filter(username__iexact=username).exists():
            return Response({"error": "username already exists."}, status=status.HTTP_400_BAD_REQUEST)
        if email and User.objects.filter(email__iexact=email).exists():
            return Response({"error": "email already exists."}, status=status.HTTP_400_BAD_REQUEST)
        valid_roles = {choice[0] for choice in AdminUserRole.ROLE_CHOICES}
        if role not in valid_roles:
            return Response({"error": "invalid role selected."}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            is_staff=True,
            is_superuser=(role == AdminUserRole.ROLE_OWNER),
        )
        AdminUserRole.objects.create(user=user, role=role)

        return Response(
            {
                "message": "Admin account created.",
                "admin": {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email,
                    "is_staff": user.is_staff,
                    "is_superuser": user.is_superuser,
                    "role": role,
                    "role_label": ROLE_LABELS.get(role, role),
                },
            },
            status=status.HTTP_201_CREATED,
        )


class AdminAccountListView(AdminAPIView):
    required_permission = "users.manage"

    def get(self, request):
        users = User.objects.filter(is_staff=True).order_by("-date_joined")
        return Response({"results": [_serialize_admin_user(user) for user in users]})


class AdminAccountDetailView(AdminAPIView):
    required_permission = "users.manage"

    def patch(self, request, user_id):
        user = User.objects.filter(id=user_id, is_staff=True).first()
        if not user:
            return Response({"error": "Admin user not found."}, status=status.HTTP_404_NOT_FOUND)

        role = (request.data.get("role") or "").strip()
        valid_roles = {choice[0] for choice in AdminUserRole.ROLE_CHOICES}
        if role not in valid_roles:
            return Response({"error": "invalid role selected."}, status=status.HTTP_400_BAD_REQUEST)

        if get_admin_role(user) == AdminUserRole.ROLE_OWNER and role != AdminUserRole.ROLE_OWNER and _owner_count() <= 1:
            return Response({"error": "Cannot remove the last owner account."}, status=status.HTTP_400_BAD_REQUEST)

        user.is_staff = True
        user.is_superuser = role == AdminUserRole.ROLE_OWNER
        user.save(update_fields=["is_staff", "is_superuser"])
        AdminUserRole.objects.update_or_create(user=user, defaults={"role": role})
        return Response({"admin": _serialize_admin_user(user)})

    def put(self, request, user_id):
        user = User.objects.filter(id=user_id, is_staff=True).first()
        if not user:
            return Response({"error": "Admin user not found."}, status=status.HTTP_404_NOT_FOUND)

        rules = normalize_special_access(request.data.get("access_rules") or {})
        policy, _ = AdminUserAccessPolicy.objects.get_or_create(user=user)
        policy.access_rules = rules
        policy.updated_by = request.admin_user
        policy.save(update_fields=["access_rules", "updated_by", "updated_at"])

        return Response({"admin": _serialize_admin_user(user)})

    def delete(self, request, user_id):
        user = User.objects.filter(id=user_id, is_staff=True).first()
        if not user:
            return Response({"error": "Admin user not found."}, status=status.HTTP_404_NOT_FOUND)
        if user.id == request.admin_user.id:
            return Response({"error": "You cannot delete your own account."}, status=status.HTTP_400_BAD_REQUEST)
        if get_admin_role(user) == AdminUserRole.ROLE_OWNER and _owner_count() <= 1:
            return Response({"error": "Cannot delete the last owner account."}, status=status.HTTP_400_BAD_REQUEST)

        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
