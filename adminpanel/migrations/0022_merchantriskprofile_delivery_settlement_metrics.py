from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("adminpanel", "0021_merchantriskprofile_scoring_counts"),
    ]

    operations = [
        migrations.AddField(
            model_name="merchantriskprofile",
            name="delivered_orders",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="merchantriskprofile",
            name="delivered_amount",
            field=models.DecimalField(decimal_places=2, default=Decimal("0"), max_digits=14),
        ),
        migrations.AddField(
            model_name="merchantriskprofile",
            name="settled_amount",
            field=models.DecimalField(decimal_places=2, default=Decimal("0"), max_digits=14),
        ),
    ]
