from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('adminpanel', '0005_contactmessage_source_topic'),
    ]

    operations = [
        migrations.CreateModel(
            name='MerchantIssue',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('merchant_id', models.IntegerField(db_index=True)),
                ('merchant_name', models.CharField(blank=True, max_length=100)),
                ('merchant_email', models.EmailField(blank=True, max_length=254)),
                ('issue_type', models.CharField(choices=[('status_mismatch', 'Delivered, but a different status is shown'), ('payment_delayed', 'Delivered, but payment is delayed'), ('payment_missing', 'Payment not received or wrong amount'), ('refund_issue', 'Refund not processed or incorrect'), ('tracking_stuck', 'Tracking is not updating'), ('rto_return', 'Return or RTO not reflected'), ('label_issue', 'Label upload rejected or flagged'), ('wrong_details', 'Wrong AWB, courier or order details'), ('customer_dispute', 'Customer says the order was not delivered'), ('other', 'Something else')], db_index=True, max_length=40)),
                ('orders', models.JSONField(blank=True, default=list)),
                ('description', models.TextField()),
                ('status', models.CharField(choices=[('new', 'New'), ('in_progress', 'In progress'), ('resolved', 'Resolved')], db_index=True, default='new', max_length=20)),
                ('admin_response', models.TextField(blank=True)),
                ('internal_note', models.TextField(blank=True)),
                ('handled_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('handled_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'admin_merchant_issue',
                'ordering': ['-created_at', '-id'],
            },
        ),
    ]
