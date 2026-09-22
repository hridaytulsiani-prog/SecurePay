from django.db import migrations


def clear_backfilled_created_at(apps, schema_editor):
    MerchantInfo = apps.get_model("payments", "MerchantInfo")
    MerchantInfo.objects.update(created_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0017_merchantinfo_created_at"),
    ]

    operations = [
        migrations.RunPython(clear_backfilled_created_at, migrations.RunPython.noop),
    ]
