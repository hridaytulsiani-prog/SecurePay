from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("payments", "0023_paymentattempt_merchantproviderprofile"),
        ("adminpanel", "0023_customerriskprofile"),
    ]

    operations = [
        migrations.CreateModel(
            name="HighValueDecisionReview",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("decision", models.CharField(choices=[("RELEASE", "Release"), ("REFUND", "Refund")], max_length=16)),
                ("amount_minor", models.PositiveIntegerField()),
                ("currency", models.CharField(default="INR", max_length=10)),
                ("status", models.CharField(choices=[("PENDING", "Pending owner review"), ("APPROVED", "Approved"), ("REJECTED", "Rejected")], db_index=True, default="PENDING", max_length=16)),
                ("reviewer_note", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("assigned_owner", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="high_value_reviews_assigned", to=settings.AUTH_USER_MODEL)),
                ("issue", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="owner_decision_reviews", to="adminpanel.merchantissue")),
                ("protected_order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="owner_decision_reviews", to="payments.paprotectedorder")),
                ("requested_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="high_value_reviews_requested", to=settings.AUTH_USER_MODEL)),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="high_value_reviews_completed", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "db_table": "admin_high_value_decision_review",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="highvaluedecisionreview",
            index=models.Index(fields=["assigned_owner", "status", "created_at"], name="admin_hv_owner_status_idx"),
        ),
        migrations.AddIndex(
            model_name="highvaluedecisionreview",
            index=models.Index(fields=["protected_order", "status"], name="admin_hv_order_status_idx"),
        ),
        migrations.AddConstraint(
            model_name="highvaluedecisionreview",
            constraint=models.UniqueConstraint(condition=models.Q(("status", "PENDING")), fields=("protected_order",), name="admin_hv_one_pending_review"),
        ),
    ]
