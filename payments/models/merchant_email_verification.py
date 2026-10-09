from django.db import models


class MerchantEmailVerification(models.Model):
    PURPOSE_REGISTER = "merchant_register"

    id = models.BigAutoField(primary_key=True)
    email = models.EmailField(db_index=True)
    purpose = models.CharField(max_length=40, default=PURPOSE_REGISTER, db_index=True)
    otp_hash = models.CharField(max_length=128)
    pending_payload = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField()
    attempt_count = models.PositiveSmallIntegerField(default=0)
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=("email", "purpose", "is_used", "created_at"), name="merchant_otp_lookup_idx"),
        ]

    def __str__(self):
        return f"{self.purpose}:{self.email}"
