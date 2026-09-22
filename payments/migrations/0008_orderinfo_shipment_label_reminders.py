from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0007_orderinfo_phonepe_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='orderinfo',
            name='shipment_label_reminder_started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='orderinfo',
            name='shipment_label_last_reminded_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='orderinfo',
            name='shipment_label_last_reminder_slot',
            field=models.CharField(blank=True, default='', max_length=16),
        ),
        migrations.AddField(
            model_name='orderinfo',
            name='shipment_label_reminder_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='orderinfo',
            name='shipment_label_escalation_email_sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
