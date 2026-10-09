from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0018_clear_merchant_created_at_backfill"),
    ]

    operations = [
        migrations.AddField(
            model_name="merchantinfo",
            name="courier_setup_completed",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="merchantinfo",
            name="courier_preferences",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
