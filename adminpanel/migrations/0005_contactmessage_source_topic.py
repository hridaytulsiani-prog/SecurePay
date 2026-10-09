from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('adminpanel', '0004_contactreply'),
    ]

    operations = [
        migrations.AddField(
            model_name='contactmessage',
            name='source',
            field=models.CharField(choices=[('customer', 'Customer'), ('merchant', 'Merchant')], db_index=True, default='customer', max_length=20),
        ),
        migrations.AddField(
            model_name='contactmessage',
            name='topic',
            field=models.CharField(blank=True, max_length=80),
        ),
    ]
