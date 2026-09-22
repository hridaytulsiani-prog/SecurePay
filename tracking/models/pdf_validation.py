from django.db import models


class PdfValidationRecord(models.Model):
    STATUS_CHOICES = [
        ("processing", "Processing"),
        ("approved", "Approved"),
        ("not_approved", "Not Approved"),
    ]

    id = models.AutoField(primary_key=True)
    merchant = models.ForeignKey('payments.MerchantInfo', on_delete=models.CASCADE, null=True, blank=True)
    file_name = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    verdict = models.CharField(max_length=64, blank=True, null=True)
    risk_verdict = models.CharField(max_length=64, blank=True, null=True)
    score = models.IntegerField(default=0)
    risk_score = models.IntegerField(default=0)
    delivery_partner = models.CharField(max_length=128, blank=True, null=True)
    courier_partner = models.CharField(max_length=128, blank=True, null=True)
    awb = models.CharField(max_length=128, blank=True, null=True)
    order_id = models.CharField(max_length=128, blank=True, null=True)
    details = models.JSONField(default=dict)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"{self.file_name} - {self.status}"
