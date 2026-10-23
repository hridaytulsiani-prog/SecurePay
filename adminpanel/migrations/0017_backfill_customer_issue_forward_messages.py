from django.db import migrations


def backfill_forwarded_messages(apps, schema_editor):
    CustomerIssue = apps.get_model("adminpanel", "CustomerIssue")
    CustomerIssueMessage = apps.get_model("adminpanel", "CustomerIssueMessage")
    PaymentNotification = apps.get_model("payments", "PaymentNotification")

    notifications = PaymentNotification.objects.filter(
        channel="dashboard",
        event_type="customer_issue_forwarded",
    )
    for notification in notifications.iterator():
        issue_id = (notification.metadata or {}).get("customer_issue_id")
        if not issue_id or not CustomerIssue.objects.filter(id=issue_id).exists():
            continue
        CustomerIssueMessage.objects.get_or_create(
            issue_id=issue_id,
            sender="admin",
            channel="merchant",
            body=notification.message,
            defaults={"admin_user_id": None},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("adminpanel", "0016_customer_issue_merchant_messages_read_at"),
        ("payments", "0022_merchantemailverification"),
    ]

    operations = [
        migrations.RunPython(backfill_forwarded_messages, migrations.RunPython.noop),
    ]
