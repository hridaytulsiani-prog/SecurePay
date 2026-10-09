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


class ContactMessage(models.Model):
    """A message sent through the public "Get in touch" form on the customer page.

    Stored here so the top-level (owner) admin can read and follow up on every
    message from the admin app's "Messages" page.
    """

    STATUS_NEW = "new"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_RESOLVED = "resolved"

    STATUS_CHOICES = [
        (STATUS_NEW, "New"),
        (STATUS_IN_PROGRESS, "In progress"),
        (STATUS_RESOLVED, "Resolved"),
    ]

    name = models.CharField(max_length=120)
    email = models.EmailField()
    order_id = models.CharField(max_length=64, blank=True)
    message = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_NEW, db_index=True)
    internal_note = models.TextField(blank=True)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    handled_at = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "admin_contact_message"
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.name} <{self.email}> ({self.status})"


class ContactReply(models.Model):
    """An email reply an admin sent to a ContactMessage from the admin app."""

    message = models.ForeignKey(ContactMessage, on_delete=models.CASCADE, related_name="replies")
    subject = models.CharField(max_length=200)
    body = models.TextField()
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "admin_contact_reply"
        ordering = ["-sent_at", "-id"]

    def __str__(self):
        return f"Reply to message {self.message_id} at {self.sent_at}"
