from django.db import models
from django.utils import timezone


class PaymentAttempt(models.Model):
    id = models.AutoField(primary_key=True)
    STATUS_CREATED = "CREATED"
    STATUS_PENDING = "PENDING"
    STATUS_SUCCESS = "SUCCESS"
    STATUS_FAILED = "FAILED"
    STATUS_CANCELLED = "CANCELLED"
    STATUS_EXPIRED = "EXPIRED"
    STATUS_UNKNOWN = "UNKNOWN"

    STATUS_CHOICES = [
        (STATUS_CREATED, "Created"),
        (STATUS_PENDING, "Pending"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_EXPIRED, "Expired"),
        (STATUS_UNKNOWN, "Unknown"),
    ]

    order = models.ForeignKey(
        "payments.OrderInfo",
        on_delete=models.CASCADE,
        related_name="payment_attempts",
    )
    checkout_session = models.ForeignKey(
        "payments.CheckoutSession",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payment_attempts",
    )
    provider = models.CharField(max_length=50, default="mock_omniware")
    provider_order_id = models.CharField(max_length=30, unique=True)
    provider_uuid = models.CharField(max_length=100, blank=True, default="")
    provider_transaction_id = models.CharField(max_length=100, blank=True, default="")
    checkout_url = models.TextField(blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_CREATED)
    response_code = models.CharField(max_length=20, blank=True, default="")
    response_message = models.TextField(blank=True, default="")
    payment_mode = models.CharField(max_length=50, blank=True, default="")
    payment_channel = models.CharField(max_length=100, blank=True, default="")
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=("provider", "status"), name="pay_attempt_provider_st_idx"),
            models.Index(fields=("provider_transaction_id",), name="pay_attempt_transaction_idx"),
        ]


class MerchantProviderProfile(models.Model):
    id = models.AutoField(primary_key=True)
    STATUS_NOT_SUBMITTED = "NOT_SUBMITTED"
    STATUS_PENDING = "PENDING"
    STATUS_APPROVED = "APPROVED"
    STATUS_REJECTED = "REJECTED"
    STATUS_DISABLED = "DISABLED"

    merchant = models.OneToOneField(
        "payments.MerchantInfo",
        on_delete=models.CASCADE,
        related_name="provider_profile",
    )
    provider = models.CharField(max_length=50, default="omniware")
    vendor_code = models.CharField(max_length=30, blank=True, default="")
    provider_account_id = models.CharField(max_length=50, blank=True, default="")
    vendor_status = models.CharField(max_length=20, default=STATUS_NOT_SUBMITTED)
    provider_response = models.JSONField(default=dict, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
