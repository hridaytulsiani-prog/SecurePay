from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("payments", "0014_pa_command_evidence"),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditLog",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("case_type", models.CharField(max_length=40)),
                ("case_id", models.CharField(max_length=120)),
                ("action", models.CharField(max_length=80)),
                ("actor_display", models.CharField(blank=True, default="", max_length=150)),
                ("actor_role", models.CharField(blank=True, default="", max_length=80)),
                ("remarks", models.TextField(blank=True, default="")),
                ("old_values", models.JSONField(blank=True, default=dict)),
                ("new_values", models.JSONField(blank=True, default=dict)),
                ("changed_fields", models.JSONField(blank=True, default=list)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("source", models.CharField(blank=True, default="", max_length=80)),
                ("request_method", models.CharField(blank=True, default="", max_length=12)),
                ("request_path", models.CharField(blank=True, default="", max_length=300)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
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
                "db_table": "audit_log",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(fields=["case_type", "case_id", "created_at"], name="audit_case_created_idx"),
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(fields=["action", "created_at"], name="audit_action_created_idx"),
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(fields=["actor_role", "created_at"], name="audit_role_created_idx"),
        ),
    ]
