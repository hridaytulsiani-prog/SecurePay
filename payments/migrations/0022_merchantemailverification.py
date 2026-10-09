from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0021_merchantinfo_checkout_field_mapping"),
    ]

    operations = [
        migrations.CreateModel(
            name="MerchantEmailVerification",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("email", models.EmailField(db_index=True, max_length=254)),
                ("purpose", models.CharField(db_index=True, default="merchant_register", max_length=40)),
                ("otp_hash", models.CharField(max_length=128)),
                ("pending_payload", models.JSONField(blank=True, default=dict)),
                ("expires_at", models.DateTimeField()),
                ("attempt_count", models.PositiveSmallIntegerField(default=0)),
                ("is_used", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["email", "purpose", "is_used", "created_at"],
                        name="merchant_otp_lookup_idx",
                    ),
                ],
            },
        ),
    ]
