from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0020_checkoutsession"),
    ]

    operations = [
        migrations.AddField(
            model_name="merchantinfo",
            name="checkout_field_mapping",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
