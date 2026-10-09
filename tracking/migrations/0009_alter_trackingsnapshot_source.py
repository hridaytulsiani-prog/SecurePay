from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tracking", "0008_trackingsnapshot_trackingapicalllog"),
    ]

    operations = [
        migrations.AlterField(
            model_name="trackingsnapshot",
            name="source",
            field=models.CharField(
                choices=[
                    ("trackparcel", "TrackParcel"),
                    ("shipsagar", "ShipSagar"),
                    ("delhivery_public", "Delhivery Public"),
                ],
                default="trackparcel",
                max_length=32,
            ),
        ),
    ]
