from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0022_merchantemailverification"),
    ]

    operations = [
        migrations.CreateModel(
            name="MerchantProviderProfile",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("provider", models.CharField(default="omniware", max_length=50)),
                ("vendor_code", models.CharField(blank=True, default="", max_length=30)),
                ("provider_account_id", models.CharField(blank=True, default="", max_length=50)),
                ("vendor_status", models.CharField(default="NOT_SUBMITTED", max_length=20)),
                ("provider_response", models.JSONField(blank=True, default=dict)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("merchant", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="provider_profile", to="payments.merchantinfo")),
            ],
        ),
        migrations.CreateModel(
            name="PaymentAttempt",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("provider", models.CharField(default="mock_omniware", max_length=50)),
                ("provider_order_id", models.CharField(max_length=30, unique=True)),
                ("provider_uuid", models.CharField(blank=True, default="", max_length=100)),
                ("provider_transaction_id", models.CharField(blank=True, default="", max_length=100)),
                ("checkout_url", models.TextField(blank=True, default="")),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("status", models.CharField(choices=[("CREATED", "Created"), ("PENDING", "Pending"), ("SUCCESS", "Success"), ("FAILED", "Failed"), ("CANCELLED", "Cancelled"), ("EXPIRED", "Expired"), ("UNKNOWN", "Unknown")], default="CREATED", max_length=20)),
                ("response_code", models.CharField(blank=True, default="", max_length=20)),
                ("response_message", models.TextField(blank=True, default="")),
                ("payment_mode", models.CharField(blank=True, default="", max_length=50)),
                ("payment_channel", models.CharField(blank=True, default="", max_length=100)),
                ("request_payload", models.JSONField(blank=True, default=dict)),
                ("response_payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("checkout_session", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="payment_attempts", to="payments.checkoutsession")),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payment_attempts", to="payments.orderinfo")),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddIndex(
            model_name="paymentattempt",
            index=models.Index(fields=["provider", "status"], name="pay_attempt_provider_st_idx"),
        ),
        migrations.AddIndex(
            model_name="paymentattempt",
            index=models.Index(fields=["provider_transaction_id"], name="pay_attempt_transaction_idx"),
        ),
    ]
