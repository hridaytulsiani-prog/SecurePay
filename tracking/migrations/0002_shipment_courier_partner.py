from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracking', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipment',
            name='courier_partner',
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
    ]
