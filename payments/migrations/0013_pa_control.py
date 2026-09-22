from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0012_refundrequest"),
    ]

    operations = [
        migrations.CreateModel(
            name="PAProviderCapability",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider", models.CharField(max_length=64, unique=True)),
                ("display_name", models.CharField(max_length=128)),
                ("supports_per_order_hold", models.BooleanField(default=False)),
                ("supports_release", models.BooleanField(default=False)),
                ("supports_refund_before_settlement", models.BooleanField(default=False)),
                ("supports_refund_after_settlement", models.BooleanField(default=False)),
                ("supports_webhooks", models.BooleanField(default=True)),
                ("supports_idempotency", models.BooleanField(default=True)),
                ("supports_status_lookup", models.BooleanField(default=True)),
                ("supports_reconciliation", models.BooleanField(default=True)),
                ("max_hold_days", models.PositiveIntegerField(default=0)),
                ("manual_review_required", models.BooleanField(default=False)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["provider"]},
        ),
        migrations.CreateModel(
            name="PAProtectedOrder",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("vaultpay_order_id", models.CharField(max_length=100, unique=True)),
                ("merchant_order_id", models.CharField(max_length=100)),
                ("pa_provider", models.CharField(max_length=64)),
                ("pa_account_id", models.CharField(blank=True, default="", max_length=100)),
                ("pa_order_id", models.CharField(blank=True, default="", max_length=128)),
                ("pa_payment_id", models.CharField(blank=True, default="", max_length=128)),
                ("amount_minor", models.PositiveIntegerField()),
                ("currency", models.CharField(default="INR", max_length=10)),
                ("protection_policy", models.CharField(default="DELIVERY_CONFIRMATION", max_length=64)),
                ("payment_state", models.CharField(default="CREATED", max_length=32)),
                ("fulfilment_state", models.CharField(default="NOT_STARTED", max_length=32)),
                ("confirmation_state", models.CharField(default="PENDING", max_length=32)),
                ("decision_state", models.CharField(default="PENDING", max_length=32)),
                ("pa_command_state", models.CharField(default="NOT_SENT", max_length=32)),
                ("protection_state", models.CharField(default="PENDING_HOLD", max_length=32)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("merchant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="pa_protected_orders", to="payments.merchantinfo")),
                ("order", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="pa_protection", to="payments.orderinfo")),
            ],
        ),
        migrations.CreateModel(
            name="PACanonicalEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_id", models.CharField(max_length=100, unique=True)),
                ("pa_provider", models.CharField(max_length=64)),
                ("pa_event_id", models.CharField(max_length=128)),
                ("event_type", models.CharField(max_length=64)),
                ("amount_minor", models.PositiveIntegerField(blank=True, null=True)),
                ("currency", models.CharField(default="INR", max_length=10)),
                ("raw_payload", models.JSONField(blank=True, default=dict)),
                ("signature_valid", models.BooleanField(default=True)),
                ("status", models.CharField(default="PROCESSED", max_length=16)),
                ("error", models.TextField(blank=True, default="")),
                ("occurred_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("protected_order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="canonical_events", to="payments.paprotectedorder")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="PAFinancialCommand",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("command_id", models.CharField(max_length=100, unique=True)),
                ("decision", models.CharField(max_length=16)),
                ("amount_minor", models.PositiveIntegerField()),
                ("currency", models.CharField(default="INR", max_length=10)),
                ("reason_code", models.CharField(max_length=64)),
                ("idempotency_key", models.CharField(max_length=180, unique=True)),
                ("provider_reference", models.CharField(max_length=128)),
                ("status", models.CharField(default="PENDING", max_length=16)),
                ("provider_request", models.JSONField(blank=True, default=dict)),
                ("provider_response", models.JSONField(blank=True, default=dict)),
                ("attempts", models.JSONField(blank=True, default=list)),
                ("error", models.TextField(blank=True, default="")),
                ("issued_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("protected_order", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="financial_command", to="payments.paprotectedorder")),
            ],
        ),
        migrations.CreateModel(
            name="PAReconciliationRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(default="NOT_RUN", max_length=16)),
                ("mismatches", models.JSONField(blank=True, default=list)),
                ("provider_report", models.JSONField(blank=True, default=dict)),
                ("last_run_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("protected_order", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="pa_reconciliation", to="payments.paprotectedorder")),
            ],
        ),
        migrations.CreateModel(
            name="PAAuditEntry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(max_length=64)),
                ("message", models.TextField()),
                ("details", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("protected_order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="pa_audit_entries", to="payments.paprotectedorder")),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddConstraint(
            model_name="pacanonicalevent",
            constraint=models.UniqueConstraint(fields=("pa_provider", "pa_event_id", "event_type"), name="pa_event_provider_event_type_uniq"),
        ),
        migrations.AddIndex(
            model_name="paprotectedorder",
            index=models.Index(fields=["merchant", "merchant_order_id"], name="pa_po_merchant_order_idx"),
        ),
        migrations.AddIndex(
            model_name="paprotectedorder",
            index=models.Index(fields=["pa_provider", "pa_payment_id"], name="pa_po_provider_pay_idx"),
        ),
        migrations.AddIndex(
            model_name="paprotectedorder",
            index=models.Index(fields=["decision_state", "pa_command_state"], name="pa_po_decision_cmd_idx"),
        ),
        migrations.AddIndex(
            model_name="pafinancialcommand",
            index=models.Index(fields=["decision", "status"], name="pa_cmd_decision_status_idx"),
        ),
        migrations.AddIndex(
            model_name="paauditentry",
            index=models.Index(fields=["protected_order", "created_at"], name="pa_audit_order_created_idx"),
        ),
    ]
