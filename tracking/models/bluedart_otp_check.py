from django.db import models
from django.utils import timezone


class BlueDartOtpCheck(models.Model):
    OTP_VERIFIED = "OTP_VERIFIED"
    CODE_VERIFIED = "CODE_VERIFIED"
    NON_OTP_DELIVERED = "NON_OTP_DELIVERED"
    UNKNOWN = "UNKNOWN"
    FETCH_FAILED = "FETCH_FAILED"

    OTP_STATUS_CHOICES = [
        (OTP_VERIFIED, "OTP Verified"),
        (CODE_VERIFIED, "Code Verified"),
        (NON_OTP_DELIVERED, "Non-OTP Delivered"),
        (UNKNOWN, "Unknown"),
        (FETCH_FAILED, "Fetch Failed"),
    ]

    id = models.BigAutoField(primary_key=True)
    awb = models.CharField(max_length=64, db_index=True)
    courier = models.CharField(max_length=32, default="bluedart")
    otp_status = models.CharField(max_length=32, choices=OTP_STATUS_CHOICES, default=UNKNOWN)
    delivery_status = models.CharField(max_length=64, blank=True)
    evidence = models.JSONField(default=list)
    raw_response = models.JSONField(null=True, blank=True)
    response_hash = models.CharField(max_length=64, blank=True)
    http_status = models.PositiveIntegerField(null=True, blank=True)
    error = models.TextField(blank=True)
    checked_by = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bluedart_otp_checks",
    )
    checked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-checked_at"]

    def __str__(self):
        return f"{self.awb} - {self.otp_status}"
