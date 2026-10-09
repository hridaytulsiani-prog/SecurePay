from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0013_pa_control"),
    ]

    operations = [
        migrations.AddField(
            model_name="pafinancialcommand",
            name="evidence_token",
            field=models.CharField(blank=True, max_length=80, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="pafinancialcommand",
            name="evidence_report_url",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
    ]
