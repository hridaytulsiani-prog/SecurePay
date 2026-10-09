from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0005_orderinfo_shipment_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='merchantinfo',
            name='password',
            field=models.CharField(default='cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e', max_length=128),
            preserve_default=False,
        ),
    ]
