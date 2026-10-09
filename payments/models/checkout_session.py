import uuid

from django.db import models


def generate_checkout_session_id():
    return f"sp_sess_{uuid.uuid4().hex}"


class CheckoutSession(models.Model):
    STATUS_CREATED = "created"
    STATUS_CONFIRMED = "confirmed"
    STATUS_PAYMENT_STARTED = "payment_started"
    STATUS_CANCELLED = "cancelled"

    id = models.AutoField(primary_key=True)
    session_id = models.CharField(max_length=64, unique=True, default=generate_checkout_session_id)
    merchant = models.ForeignKey("payments.MerchantInfo", on_delete=models.CASCADE)
    merchant_key = models.CharField(max_length=100)
    customer_snapshot = models.JSONField(default=dict, blank=True)
    order_snapshot = models.JSONField(default=dict, blank=True)
    page_snapshot = models.JSONField(default=dict, blank=True)
    source_origin = models.CharField(max_length=255, blank=True, default="")
    source_url = models.TextField(blank=True, default="")
    customer_user_agent = models.TextField(blank=True, default="")
    customer_ip = models.GenericIPAddressField(null=True, blank=True)
    status = models.CharField(max_length=32, default=STATUS_CREATED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
