from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [("payments", "0011_payment_lifecycle")]

    operations = [
        migrations.CreateModel(
            name="RefundRequest",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("provider", models.CharField(max_length=64)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("currency", models.CharField(default="INR", max_length=10)),
                ("reason", models.CharField(max_length=255)),
                ("refund_reference", models.CharField(max_length=100, unique=True)),
                ("provider_payment_id", models.CharField(blank=True, default="", max_length=255)),
                ("provider_refund_id", models.CharField(blank=True, default="", max_length=255)),
                ("status", models.CharField(choices=[("REQUESTED", "Requested"), ("PENDING", "Pending"), ("COMPLETED", "Completed"), ("FAILED", "Failed"), ("REJECTED", "Rejected")], default="REQUESTED", max_length=16)),
                ("verification_snapshot", models.JSONField(default=dict)),
                ("provider_request", models.JSONField(default=dict)),
                ("provider_response", models.JSONField(default=dict)),
                ("error", models.TextField(blank=True, default="")),
                ("requested_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("provider_submitted_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("order", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="refund_request", to="payments.orderinfo")),
            ],
            options={
                "indexes": [
                    models.Index(fields=["provider", "provider_refund_id"], name="pay_rr_provider_refund_idx"),
                    models.Index(fields=["status", "requested_at"], name="pay_rr_status_req_idx"),
                ],
            },
        ),
    ]
