from django.db import migrations, models
import django.db.models.deletion
import payments.models.checkout_session


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0019_merchant_courier_setup"),
    ]

    operations = [
        migrations.CreateModel(
            name="CheckoutSession",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("session_id", models.CharField(default=payments.models.checkout_session.generate_checkout_session_id, max_length=64, unique=True)),
                ("merchant_key", models.CharField(max_length=100)),
                ("customer_snapshot", models.JSONField(blank=True, default=dict)),
                ("order_snapshot", models.JSONField(blank=True, default=dict)),
                ("page_snapshot", models.JSONField(blank=True, default=dict)),
                ("source_origin", models.CharField(blank=True, default="", max_length=255)),
                ("source_url", models.TextField(blank=True, default="")),
                ("customer_user_agent", models.TextField(blank=True, default="")),
                ("customer_ip", models.GenericIPAddressField(blank=True, null=True)),
                ("status", models.CharField(default="created", max_length=32)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("merchant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="payments.merchantinfo")),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
