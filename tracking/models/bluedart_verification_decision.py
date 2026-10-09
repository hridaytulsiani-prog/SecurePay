from django.db import models
from django.utils import timezone


class BlueDartVerificationDecision(models.Model):
    VERIFIED = "VERIFIED"
    NOT_VERIFIED = "NOT_VERIFIED"

    DECISION_CHOICES = [
        (VERIFIED, "Verified"),
        (NOT_VERIFIED, "Not Verified"),
    ]

    id = models.BigAutoField(primary_key=True)
    awb = models.CharField(max_length=64, db_index=True)
    decision = models.CharField(max_length=24, choices=DECISION_CHOICES)
    source_check = models.ForeignKey(
        "tracking.BlueDartOtpCheck",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="manual_decisions",
    )
    decided_by = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bluedart_verification_decisions",
    )
    decided_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-decided_at"]

    def __str__(self):
        return f"{self.awb} - {self.decision}"
