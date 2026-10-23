from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("adminpanel", "0015_customer_issue_message_channel"),
    ]

    operations = [
        migrations.AddField(
            model_name="customerissue",
            name="merchant_messages_read_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
