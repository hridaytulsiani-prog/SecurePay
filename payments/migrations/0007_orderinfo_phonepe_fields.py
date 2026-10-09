from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        ('payments', '0006_merchantinfo_password'),
    ]

    operations = [
        migrations.CreateModel(
            name='EnquiryData',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('enquiry_id', models.CharField(max_length=100, unique=True)),
                ('order_id', models.CharField(max_length=100)),
                ('enquiry_text', models.TextField()),
                ('receipt_status', models.CharField(choices=[('received', 'Received'), ('not_received', 'Not Received'), ('wrong_order', 'Wrong Order')], max_length=20)),
                ('someone_else_received', models.BooleanField(blank=True, null=True)),
                ('agent_contacted', models.BooleanField(blank=True, null=True)),
                ('otp_shared', models.BooleanField(blank=True, null=True)),
                ('unboxing_evidence', models.BooleanField(blank=True, null=True)),
                ('evidence_file', models.FileField(blank=True, null=True, upload_to='enquiry_evidence/')),
                ('status', models.CharField(default='pending', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('resolution_status', models.CharField(choices=[('unresolved', 'Unresolved'), ('money_refunded', 'Money Refunded'), ('money_to_merchant', 'Money To Merchant')], default='unresolved', max_length=20)),
                ('resolution_reason', models.TextField(blank=True, null=True)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('resolved_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='auth.user')),
            ],
            options={
                'db_table': 'enquiry_data',
            },
        ),
        migrations.CreateModel(
            name='EnquiryNote',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('note', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='auth.user')),
                ('enquiry', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='notes', to='payments.enquirydata')),
            ],
            options={
                'db_table': 'enquiry_note',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddField(
            model_name='orderinfo',
            name='phonepe_order_id',
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
        migrations.AddField(
            model_name='orderinfo',
            name='phonepe_payment_id',
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
        migrations.AddField(
            model_name='orderinfo',
            name='phonepe_raw_response',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='orderinfo',
            name='payment_provider',
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
        migrations.AddField(
            model_name='orderinfo',
            name='enquiry',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='payments.enquirydata'),
        ),
    ]
