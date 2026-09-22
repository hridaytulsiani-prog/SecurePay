from django.db import models
from django.utils import timezone

from tracking.models.trackinginfo import Shipment


class TrackingSnapshot(models.Model):
    SOURCE_TRACKPARCEL = "trackparcel"
    SOURCE_SHIPSAGAR = "shipsagar"
    SOURCE_DELHIVERY_PUBLIC = "delhivery_public"

    SOURCE_CHOICES = [
        (SOURCE_TRACKPARCEL, "TrackParcel"),
        (SOURCE_SHIPSAGAR, "ShipSagar"),
        (SOURCE_DELHIVERY_PUBLIC, "Delhivery Public"),
    ]

    shipment = models.OneToOneField(
        Shipment,
        on_delete=models.CASCADE,
        related_name="tracking_snapshot",
    )
    awb = models.CharField(max_length=64, db_index=True)
    courier = models.CharField(max_length=128, blank=True, null=True)
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES, default=SOURCE_TRACKPARCEL)
    current_status = models.CharField(max_length=128, blank=True)
    normalized_status = models.CharField(max_length=64, blank=True)
    expected_delivery_date = models.DateField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    next_check_after = models.DateTimeField(null=True, blank=True)
    raw_response = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.awb} - {self.normalized_status or self.current_status}"


class TrackingApiCallLog(models.Model):
    PROVIDER_TRACKPARCEL = "trackparcel"
    PROVIDER_SHIPSAGAR = "shipsagar"

    provider = models.CharField(max_length=32, default=PROVIDER_TRACKPARCEL, db_index=True)
    shipment = models.ForeignKey(
        Shipment,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tracking_api_calls",
    )
    awb = models.CharField(max_length=64, db_index=True)
    courier = models.CharField(max_length=128, blank=True, null=True)
    endpoint = models.CharField(max_length=255, blank=True)
    http_status = models.PositiveIntegerField(null=True, blank=True)
    success = models.BooleanField(default=False)
    request_payload = models.JSONField(null=True, blank=True)
    response_payload = models.JSONField(null=True, blank=True)
    response_headers = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    called_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-called_at"]

    def __str__(self):
        return f"{self.provider} {self.awb} {self.http_status or '-'}"
