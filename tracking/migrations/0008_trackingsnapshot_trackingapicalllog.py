from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("tracking", "0007_delhiveryverificationdecision"),
    ]

    operations = [
        migrations.CreateModel(
            name="TrackingSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("awb", models.CharField(db_index=True, max_length=64)),
                ("courier", models.CharField(blank=True, max_length=128, null=True)),
                (
                    "source",
                    models.CharField(
                        choices=[("trackparcel", "TrackParcel"), ("shipsagar", "ShipSagar")],
                        default="trackparcel",
                        max_length=32,
                    ),
                ),
                ("current_status", models.CharField(blank=True, max_length=128)),
                ("normalized_status", models.CharField(blank=True, max_length=64)),
                ("expected_delivery_date", models.DateField(blank=True, null=True)),
                ("delivered_at", models.DateTimeField(blank=True, null=True)),
                ("last_checked_at", models.DateTimeField(blank=True, null=True)),
                ("next_check_after", models.DateTimeField(blank=True, null=True)),
                ("raw_response", models.JSONField(blank=True, null=True)),
                ("error", models.TextField(blank=True)),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "shipment",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tracking_snapshot",
                        to="tracking.shipment",
                    ),
                ),
            ],
            options={"ordering": ["-updated_at"]},
        ),
        migrations.CreateModel(
            name="TrackingApiCallLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider", models.CharField(db_index=True, default="trackparcel", max_length=32)),
                ("awb", models.CharField(db_index=True, max_length=64)),
                ("courier", models.CharField(blank=True, max_length=128, null=True)),
                ("endpoint", models.CharField(blank=True, max_length=255)),
                ("http_status", models.PositiveIntegerField(blank=True, null=True)),
                ("success", models.BooleanField(default=False)),
                ("request_payload", models.JSONField(blank=True, null=True)),
                ("response_payload", models.JSONField(blank=True, null=True)),
                ("response_headers", models.JSONField(blank=True, default=dict)),
                ("error", models.TextField(blank=True)),
                ("called_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                (
                    "shipment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="tracking_api_calls",
                        to="tracking.shipment",
                    ),
                ),
            ],
            options={"ordering": ["-called_at"]},
        ),
    ]
