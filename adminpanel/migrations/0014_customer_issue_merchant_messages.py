import adminpanel.models
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("adminpanel", "0013_customerissue_details"),
    ]

    operations = [
        migrations.AlterField(
            model_name="customerissuemessage",
            name="sender",
            field=models.CharField(
                choices=[("customer", "Customer"), ("admin", "EscroSafe"), ("merchant", "Merchant")],
                db_index=True,
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="CustomerIssueMessageAttachment",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("file", models.FileField(upload_to=adminpanel.models.customer_issue_message_upload_path)),
                ("original_name", models.CharField(blank=True, max_length=200)),
                ("content_type", models.CharField(blank=True, max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("message", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="attachments", to="adminpanel.customerissuemessage")),
            ],
            options={
                "db_table": "admin_customer_issue_message_attachment",
                "ordering": ["created_at", "id"],
            },
        ),
    ]
