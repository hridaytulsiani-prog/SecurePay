from django.conf import settings
from django.db import models


class AdminUserRole(models.Model):
    ROLE_OWNER = "owner"
    ROLE_OPERATIONS = "operations"
    ROLE_AUDITOR = "auditor"

    ROLE_CHOICES = [
        (ROLE_OWNER, "Owner"),
        (ROLE_OPERATIONS, "Operations Manager"),
        (ROLE_AUDITOR, "Auditor / Viewer"),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="admin_role")
    role = models.CharField(max_length=30, choices=ROLE_CHOICES, default=ROLE_OPERATIONS)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "admin_user_role"

    def __str__(self):
        return f"{self.user_id} - {self.role}"


class AdminUserAccessPolicy(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="admin_access_policy")
    access_rules = models.JSONField(default=dict, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "admin_user_access_policy"

    def __str__(self):
        return f"{self.user_id} access policy"
