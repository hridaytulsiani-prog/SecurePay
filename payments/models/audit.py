from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class AuditLog(models.Model):
    """Append-only record of sensitive business actions."""

    CASE_ENQUIRY = "enquiry"
    CASE_ORDER = "order"
    CASE_REFUND = "refund"
    CASE_PAYMENT = "payment"

    id = models.AutoField(primary_key=True)
    case_type = models.CharField(max_length=40)
    case_id = models.CharField(max_length=120)
    action = models.CharField(max_length=80)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    actor_display = models.CharField(max_length=150, blank=True, default="")
    actor_role = models.CharField(max_length=80, blank=True, default="")
    remarks = models.TextField(blank=True, default="")
    old_values = models.JSONField(default=dict, blank=True)
    new_values = models.JSONField(default=dict, blank=True)
    changed_fields = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    source = models.CharField(max_length=80, blank=True, default="")
    request_method = models.CharField(max_length=12, blank=True, default="")
    request_path = models.CharField(max_length=300, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "audit_log"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=("case_type", "case_id", "created_at"), name="audit_case_created_idx"),
            models.Index(fields=("action", "created_at"), name="audit_action_created_idx"),
            models.Index(fields=("actor_role", "created_at"), name="audit_role_created_idx"),
        ]

    def save(self, *args, **kwargs):
        if self.pk and AuditLog.objects.filter(pk=self.pk).exists():
            raise ValidationError("Audit logs are immutable and cannot be edited.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit logs are immutable and cannot be deleted.")

    def __str__(self):
        return f"{self.case_type}:{self.case_id} {self.action}"
