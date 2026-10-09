from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def copy_existing_notes(apps, schema_editor):
    """Each issue's single internal note becomes the first entry of its note log."""
    MerchantIssue = apps.get_model('adminpanel', 'MerchantIssue')
    MerchantIssueNote = apps.get_model('adminpanel', 'MerchantIssueNote')
    for issue in MerchantIssue.objects.exclude(internal_note=''):
        note = MerchantIssueNote.objects.create(issue=issue, note=issue.internal_note, admin_user=issue.handled_by)
        MerchantIssueNote.objects.filter(id=note.id).update(created_at=issue.handled_at or issue.updated_at)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('adminpanel', '0009_merchantissue_report'),
    ]

    operations = [
        migrations.CreateModel(
            name='MerchantIssueNote',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('note', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('admin_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('issue', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='notes', to='adminpanel.merchantissue')),
            ],
            options={
                'db_table': 'admin_merchant_issue_note',
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.RunPython(copy_existing_notes, migrations.RunPython.noop),
    ]
