from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("adminpanel", "0019_remove_remaining_customer_issue_forward_messages"),
        ("payments", "0022_merchantemailverification"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MerchantRiskProfile",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("success_rate", models.DecimalField(decimal_places=2, default=100, max_digits=5)),
                ("total_orders", models.PositiveIntegerField(default=0)),
                ("successful_orders", models.PositiveIntegerField(default=0)),
                ("unsuccessful_orders", models.PositiveIntegerField(default=0)),
                ("pending_orders", models.PositiveIntegerField(default=0)),
                ("score_updated_at", models.DateTimeField(blank=True, null=True)),
                ("access_status", models.CharField(choices=[("active", "Active"), ("temporary_block", "Temporarily blocked"), ("permanent_block", "Permanently blocked")], db_index=True, default="active", max_length=24)),
                ("blocked_until", models.DateTimeField(blank=True, null=True)),
                ("block_reason", models.TextField(blank=True)),
                ("blocked_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("blocked_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("merchant", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="risk_profile", to="payments.merchantinfo")),
            ],
            options={
                "db_table": "admin_merchant_risk_profile",
                "ordering": ["success_rate", "merchant_id"],
            },
        ),
    ]
