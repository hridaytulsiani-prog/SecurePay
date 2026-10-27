from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("adminpanel", "0022_merchantriskprofile_delivery_settlement_metrics"),
        ("payments", "0022_merchantemailverification"),
    ]

    operations = [
        migrations.CreateModel(
            name="CustomerRiskProfile",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("success_rate", models.DecimalField(decimal_places=2, default=100, max_digits=5)),
                ("total_orders", models.PositiveIntegerField(default=0)),
                ("assessed_orders", models.PositiveIntegerField(default=0)),
                ("clean_orders", models.PositiveIntegerField(default=0)),
                ("problem_orders", models.PositiveIntegerField(default=0)),
                ("issue_count", models.PositiveIntegerField(default=0)),
                ("pending_orders", models.PositiveIntegerField(default=0)),
                ("score_updated_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("customer", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="risk_profile", to="payments.customerinfo")),
            ],
            options={
                "db_table": "admin_customer_risk_profile",
                "ordering": ["success_rate", "-problem_orders", "customer_id"],
            },
        ),
    ]
