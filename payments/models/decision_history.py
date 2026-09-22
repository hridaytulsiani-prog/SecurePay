from django.conf import settings
from django.db import models
from django.utils import timezone


class DecisionHistory(models.Model):
    """Append-only date-wise journey for a case decision/status."""

    id = models.AutoField(primary_key=True)
    case_type = models.CharField(max_length=40)
    case_id = models.CharField(max_length=120)
    order_id = models.CharField(max_length=120, blank=True, default="")
    status = models.CharField(max_length=80)
    title = models.CharField(max_length=160)
    remarks = models.TextField(blank=True, default="")
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    actor_display = models.CharField(max_length=150, blank=True, default="")
    actor_role = models.CharField(max_length=80, blank=True, default="")
    version = models.PositiveIntegerField(default=1)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "decision_history"
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=("case_type", "case_id", "created_at"), name="decision_case_created_idx"),
            models.Index(fields=("order_id", "created_at"), name="decision_order_created_idx"),
        ]

    def __str__(self):
        return f"{self.case_type}:{self.case_id} v{self.version} {self.status}"
