from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("payments", "0015_auditlog"),
    ]

    operations = [
        migrations.CreateModel(
            name="DecisionHistory",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("case_type", models.CharField(max_length=40)),
                ("case_id", models.CharField(max_length=120)),
                ("order_id", models.CharField(blank=True, default="", max_length=120)),
                ("status", models.CharField(max_length=80)),
                ("title", models.CharField(max_length=160)),
                ("remarks", models.TextField(blank=True, default="")),
                ("actor_display", models.CharField(blank=True, default="", max_length=150)),
                ("actor_role", models.CharField(blank=True, default="", max_length=80)),
                ("version", models.PositiveIntegerField(default=1)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "actor_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "decision_history",
                "ordering": ["created_at", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="decisionhistory",
            index=models.Index(fields=["case_type", "case_id", "created_at"], name="decision_case_created_idx"),
        ),
        migrations.AddIndex(
            model_name="decisionhistory",
            index=models.Index(fields=["order_id", "created_at"], name="decision_order_created_idx"),
        ),
    ]
