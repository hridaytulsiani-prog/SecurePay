from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def backfill_payment_states(apps, schema_editor):
    OrderInfo = apps.get_model("payments", "OrderInfo")
    OrderInfo.objects.filter(order_status__iexact="paid").update(payment_state="PAYMENT_CAPTURED")
    OrderInfo.objects.filter(order_status__iexact="pending").update(payment_state="PAYMENT_PENDING")
    OrderInfo.objects.filter(order_status__iexact="payment_failed").update(payment_state="PAYMENT_FAILED")


class Migration(migrations.Migration):
    dependencies = [("payments", "0010_alter_orderinfo_shipment_label_last_reminder_slot")]

    operations = [
        migrations.AddField(
            model_name="orderinfo",
            name="payment_state",
            field=models.CharField(default="CREATED", max_length=32),
        ),
        migrations.AddField(
            model_name="orderinfo",
            name="payment_state_metadata",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="orderinfo",
            name="payment_state_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_payment_states, migrations.RunPython.noop),
        migrations.CreateModel(
            name="PaymentEvent",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("provider", models.CharField(max_length=64)),
                ("event_id", models.CharField(blank=True, default="", max_length=255)),
                ("event_hash", models.CharField(max_length=64)),
                ("event_type", models.CharField(blank=True, default="", max_length=128)),
                ("payload", models.JSONField(default=dict)),
                ("signature", models.CharField(blank=True, default="", max_length=512)),
                ("status", models.CharField(default="RECEIVED", max_length=16)),
                ("error", models.TextField(blank=True, default="")),
                ("received_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("order", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="payment_events", to="payments.orderinfo")),
            ],
            options={
                "indexes": [models.Index(fields=["provider", "event_id"], name="payments_pe_provider_event_idx"), models.Index(fields=["status", "received_at"], name="pay_pe_status_recv_idx")],
                "constraints": [models.UniqueConstraint(fields=("provider", "event_hash"), name="payment_event_provider_hash_uniq")],
            },
        ),
        migrations.CreateModel(
            name="PaymentStateTransition",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("from_state", models.CharField(blank=True, default="", max_length=32)),
                ("to_state", models.CharField(max_length=32)),
                ("source", models.CharField(default="system", max_length=64)),
                ("metadata", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("event", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="state_transitions", to="payments.paymentevent")),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payment_state_transitions", to="payments.orderinfo")),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.CreateModel(
            name="PaymentLedgerEntry",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("entry_type", models.CharField(max_length=32)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("currency", models.CharField(default="INR", max_length=10)),
                ("reference", models.CharField(blank=True, default="", max_length=255)),
                ("metadata", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ledger_entries", to="payments.orderinfo")),
            ],
            options={"indexes": [models.Index(fields=["order", "entry_type", "created_at"], name="payments_ple_order_type_idx")]},
        ),
        migrations.CreateModel(
            name="PaymentNotification",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("event_type", models.CharField(max_length=64)),
                ("channel", models.CharField(default="DASHBOARD", max_length=16)),
                ("dedupe_key", models.CharField(max_length=255, unique=True)),
                ("title", models.CharField(max_length=255)),
                ("message", models.TextField()),
                ("status", models.CharField(default="PENDING", max_length=16)),
                ("metadata", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("read_at", models.DateTimeField(blank=True, null=True)),
                ("merchant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payment_notifications", to="payments.merchantinfo")),
                ("order", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="payment_notifications", to="payments.orderinfo")),
            ],
            options={"ordering": ["-created_at"], "indexes": [models.Index(fields=["merchant", "status", "created_at"], name="pay_pn_mer_status_idx")]},
        ),
        migrations.CreateModel(
            name="SettlementRecord",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("provider", models.CharField(max_length=64)),
                ("settlement_id", models.CharField(blank=True, default="", max_length=255)),
                ("status", models.CharField(default="PENDING", max_length=16)),
                ("gross_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("fee_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("net_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("eligible_at", models.DateTimeField(blank=True, null=True)),
                ("settled_at", models.DateTimeField(blank=True, null=True)),
                ("last_checked_at", models.DateTimeField(blank=True, null=True)),
                ("raw_response", models.JSONField(blank=True, null=True)),
                ("error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("order", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="settlement_record", to="payments.orderinfo")),
            ],
            options={"indexes": [models.Index(fields=["status", "eligible_at"], name="pay_sr_status_elig_idx")]},
        ),
    ]
