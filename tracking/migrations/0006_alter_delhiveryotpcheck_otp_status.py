from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tracking", "0005_delhiveryotpcheck"),
    ]

    operations = [
        migrations.AlterField(
            model_name="delhiveryotpcheck",
            name="otp_status",
            field=models.CharField(
                choices=[
                    ("OTP_VERIFIED", "OTP Verified"),
                    ("CODE_VERIFIED", "Code Verified"),
                    ("NON_OTP_DELIVERED", "Non-OTP Delivered"),
                    ("UNKNOWN", "Unknown"),
                    ("FETCH_FAILED", "Fetch Failed"),
                ],
                default="UNKNOWN",
                max_length=32,
            ),
        ),
    ]
