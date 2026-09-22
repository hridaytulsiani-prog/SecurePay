from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tracking", "0003_pdfvalidationrecord"),
    ]

    operations = [
        migrations.AlterField(
            model_name="pdfvalidationrecord",
            name="status",
            field=models.CharField(
                choices=[
                    ("processing", "Processing"),
                    ("approved", "Approved"),
                    ("not_approved", "Not Approved"),
                ],
                max_length=20,
            ),
        ),
    ]
