from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0007_orderinfo_phonepe_fields'),
        ('tracking', '0002_shipment_courier_partner'),
    ]

    operations = [
        migrations.CreateModel(
            name='PdfValidationRecord',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file_name', models.CharField(max_length=255)),
                ('status', models.CharField(choices=[('approved', 'Approved'), ('not_approved', 'Not Approved')], max_length=20)),
                ('verdict', models.CharField(blank=True, max_length=64, null=True)),
                ('risk_verdict', models.CharField(blank=True, max_length=64, null=True)),
                ('score', models.IntegerField(default=0)),
                ('risk_score', models.IntegerField(default=0)),
                ('delivery_partner', models.CharField(blank=True, max_length=128, null=True)),
                ('courier_partner', models.CharField(blank=True, max_length=128, null=True)),
                ('awb', models.CharField(blank=True, max_length=128, null=True)),
                ('order_id', models.CharField(blank=True, max_length=128, null=True)),
                ('details', models.JSONField(default=dict)),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('merchant', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='payments.merchantinfo')),
            ],
            options={
                'ordering': ['-uploaded_at'],
            },
        ),
    ]
