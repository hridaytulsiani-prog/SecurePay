from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def copy_existing_conversation(apps, schema_editor):
    """Turn each existing issue's description and admin response into the first chat messages."""
    MerchantIssue = apps.get_model('adminpanel', 'MerchantIssue')
    MerchantIssueMessage = apps.get_model('adminpanel', 'MerchantIssueMessage')
    for issue in MerchantIssue.objects.all():
        first = MerchantIssueMessage.objects.create(issue=issue, sender='merchant', body=issue.description)
        MerchantIssueMessage.objects.filter(id=first.id).update(created_at=issue.created_at)
        if issue.admin_response:
            reply = MerchantIssueMessage.objects.create(
                issue=issue, sender='admin', body=issue.admin_response, admin_user=issue.handled_by
            )
            MerchantIssueMessage.objects.filter(id=reply.id).update(created_at=issue.handled_at or issue.updated_at)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('adminpanel', '0006_merchantissue'),
    ]

    operations = [
        migrations.CreateModel(
            name='MerchantIssueMessage',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sender', models.CharField(choices=[('merchant', 'Merchant'), ('admin', 'EscroSafe')], db_index=True, max_length=20)),
                ('body', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('admin_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('issue', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='messages', to='adminpanel.merchantissue')),
            ],
            options={
                'db_table': 'admin_merchant_issue_message',
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.RunPython(copy_existing_conversation, migrations.RunPython.noop),
    ]
