from django.db import migrations


def remove_forwarded_messages(apps, schema_editor):
    CustomerIssueMessage = apps.get_model("adminpanel", "CustomerIssueMessage")
    PaymentNotification = apps.get_model("payments", "PaymentNotification")

    notifications = PaymentNotification.objects.filter(
        channel__iexact="dashboard",
        event_type="customer_issue_forwarded",
    )
    for notification in notifications.iterator():
        issue_id = (notification.metadata or {}).get("customer_issue_id")
        if not issue_id:
            continue
        CustomerIssueMessage.objects.filter(
            issue_id=issue_id,
            sender="admin",
            channel="merchant",
            body=notification.message,
        ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("adminpanel", "0018_remove_customer_issue_forward_messages"),
    ]

    operations = [
        migrations.RunPython(remove_forwarded_messages, migrations.RunPython.noop),
    ]
