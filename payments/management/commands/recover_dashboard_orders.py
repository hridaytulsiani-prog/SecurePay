from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from payments.models import CustomerInfo, MerchantInfo, OrderInfo, PAProtectedOrder
from tracking.models.trackinginfo import Shipment


class Command(BaseCommand):
    help = "Recover merchant dashboard OrderInfo rows from PAProtectedOrder records."

    def add_arguments(self, parser):
        parser.add_argument("--merchant-email", required=True, help="Merchant email to recover orders for.")

    def handle(self, *args, **options):
        merchant = MerchantInfo.objects.filter(merchant_email=options["merchant_email"]).first()
        if not merchant:
            raise CommandError(f"No merchant found with email {options['merchant_email']}.")

        default_customer, _ = CustomerInfo.objects.update_or_create(
            customer_phone="9999999999",
            defaults={
                "customer_name": "Demo Customer",
                "customer_email": "demo.customer@example.com",
                "customer_address": "Recovered dashboard order",
            },
        )

        created_count = 0
        updated_count = 0

        for protected_order in PAProtectedOrder.objects.filter(merchant=merchant).order_by("id"):
            pa_order_id = protected_order.pa_order_id or protected_order.merchant_order_id
            shipment = (
                Shipment.objects.filter(pa_order_id=pa_order_id).first()
                or Shipment.objects.filter(awb=pa_order_id).first()
            )

            order_status = self._order_status(protected_order.payment_state)
            amount = Decimal(protected_order.amount_minor) / Decimal("100")

            _, created = OrderInfo.objects.update_or_create(
                merchant=merchant,
                merchant_order_id=protected_order.merchant_order_id,
                defaults={
                    "pa_order_id": pa_order_id,
                    "pa_payment_id": protected_order.pa_payment_id,
                    "phonepe_order_id": pa_order_id,
                    "phonepe_payment_id": protected_order.pa_payment_id,
                    "order_amount": amount,
                    "order_currency": protected_order.currency or "INR",
                    "order_status": order_status,
                    "payment_state": protected_order.payment_state,
                    "payment_state_updated_at": protected_order.updated_at or timezone.now(),
                    "payment_state_metadata": {
                        "recovered_from": "PAProtectedOrder",
                        "protected_order_id": protected_order.id,
                    },
                    "customer_info": default_customer,
                    "shipment_id": shipment,
                    "payment_provider": protected_order.pa_provider or "MockPay",
                },
            )

            if created:
                created_count += 1
            else:
                updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Recovered dashboard orders for {merchant.merchant_email}: "
                f"{created_count} created, {updated_count} updated."
            )
        )

    def _order_status(self, payment_state):
        normalized = str(payment_state or "").upper()
        if normalized in {"SUCCEEDED", "SUCCESS", "PAID", "PAYMENT_CAPTURED", "SETTLED", "HELD"}:
            return "PAID"
        if normalized in {"REFUNDED", "REFUND"}:
            return "REFUNDED"
        if normalized in {"FAILED", "CANCELLED"}:
            return "FAILED"
        return "PENDING"
