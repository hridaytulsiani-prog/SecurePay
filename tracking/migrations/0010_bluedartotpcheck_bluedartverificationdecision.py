from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("tracking", "0009_alter_trackingsnapshot_source"),
    ]

    operations = [
        migrations.CreateModel(
            name="BlueDartOtpCheck",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("awb", models.CharField(db_index=True, max_length=64)),
                ("courier", models.CharField(default="bluedart", max_length=32)),
                (
                    "otp_status",
                    models.CharField(
                        choices=[
                            ("OTP_VERIFIED", "OTP Verified"),
                            ("CODE_VERIFIED", "Code Verified"),
                            ("NON_OTP_DELIVERED", "Non-OTP Delivered"),
                            ("UNKNOWN", "Unknown"),
                            ("FETCH_FAILED", "Fetch Failed"),
                        ],
                        default="UNKNOWN",
                        max_length=32,
                    ),
                ),
                ("delivery_status", models.CharField(blank=True, max_length=64)),
                ("evidence", models.JSONField(default=list)),
                ("raw_response", models.JSONField(blank=True, null=True)),
                ("response_hash", models.CharField(blank=True, max_length=64)),
                ("http_status", models.PositiveIntegerField(blank=True, null=True)),
                ("error", models.TextField(blank=True)),
                ("checked_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "checked_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="bluedart_otp_checks",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-checked_at"]},
        ),
        migrations.CreateModel(
            name="BlueDartVerificationDecision",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("awb", models.CharField(db_index=True, max_length=64)),
                (
                    "decision",
                    models.CharField(
                        choices=[("VERIFIED", "Verified"), ("NOT_VERIFIED", "Not Verified")],
                        max_length=24,
                    ),
                ),
                ("decided_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "decided_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="bluedart_verification_decisions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "source_check",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="manual_decisions",
                        to="tracking.bluedartotpcheck",
                    ),
                ),
            ],
            options={"ordering": ["-decided_at"]},
        ),
    ]
