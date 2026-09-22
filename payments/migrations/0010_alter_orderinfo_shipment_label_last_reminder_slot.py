from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0008_orderinfo_shipment_label_reminders"),
    ]

    operations = [
        migrations.AlterField(
            model_name="orderinfo",
            name="shipment_label_last_reminder_slot",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
