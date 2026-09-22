from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("tracking", "0006_alter_delhiveryotpcheck_otp_status"),
    ]

    operations = [
        migrations.CreateModel(
            name="DelhiveryVerificationDecision",
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
                        related_name="delhivery_verification_decisions",
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
                        to="tracking.delhiveryotpcheck",
                    ),
                ),
            ],
            options={"ordering": ["-decided_at"]},
        ),
    ]
