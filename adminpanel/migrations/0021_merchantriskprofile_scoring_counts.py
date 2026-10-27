from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("adminpanel", "0020_merchantriskprofile"),
    ]

    operations = [
        migrations.AddField(
            model_name="merchantriskprofile",
            name="scored_orders",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="merchantriskprofile",
            name="excluded_orders",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
