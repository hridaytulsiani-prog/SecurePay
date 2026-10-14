from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tracking", "0010_bluedartotpcheck_bluedartverificationdecision"),
    ]

    operations = [
        migrations.CreateModel(
            name="WhatsAppMessageLog",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("to_number", models.CharField(db_index=True, max_length=20)),
                ("purpose", models.CharField(blank=True, max_length=50)),
                ("order_id", models.CharField(blank=True, db_index=True, max_length=100)),
                ("message_body", models.TextField()),
                (
                    "status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("sent", "Sent"), ("failed", "Failed")],
                        default="pending",
                        max_length=10,
                    ),
                ),
                ("error", models.TextField(blank=True, null=True)),
                ("wa_id", models.CharField(blank=True, max_length=32)),
                ("wa_message_id", models.CharField(blank=True, db_index=True, max_length=128)),
                ("raw_response", models.JSONField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "whatsapp_message_log",
                "ordering": ["-created_at"],
            },
        ),
    ]
