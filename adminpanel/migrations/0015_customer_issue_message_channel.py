from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("adminpanel", "0014_customer_issue_merchant_messages"),
    ]

    operations = [
        migrations.AddField(
            model_name="customerissuemessage",
            name="channel",
            field=models.CharField(
                choices=[("customer", "Customer conversation"), ("merchant", "Merchant conversation")],
                db_index=True,
                default="customer",
                max_length=20,
            ),
        ),
    ]
