from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0016_decisionhistory"),
    ]

    operations = [
        migrations.AddField(
            model_name="merchantinfo",
            name="created_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="merchantinfo",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, blank=True, null=True),
        ),
    ]
