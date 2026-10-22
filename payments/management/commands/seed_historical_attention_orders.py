from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from payments.models import CustomerInfo, MerchantInfo, OrderInfo


HISTORICAL_ORDERS = [
    ("SP-DEMO-PENDING-07D", "7814529630", 7, "Riya Verma", "9876501707", "1299.00"),
    ("SP-DEMO-PENDING-14D", "6402981753", 14, "Arjun Patel", "9876501714", "1899.00"),
    ("SP-DEMO-PENDING-30D", "9351748206", 30, "Sneha Joshi", "9876501730", "2499.00"),
    ("SP-DEMO-PENDING-60D", "5278604193", 60, "Vikram Das", "9876501760", "3499.00"),
]


class Command(BaseCommand):
    help = "Seed unresolved historical demo orders for the admin attention queue."

    def add_arguments(self, parser):
        parser.add_argument("--merchant-email", help="Merchant email to seed orders for.")

    def handle(self, *args, **options):
        merchant_email = options.get("merchant_email")
        merchant = (
            MerchantInfo.objects.filter(merchant_email=merchant_email).first()
            if merchant_email
            else MerchantInfo.objects.order_by("id").first()
        )
        if not merchant:
            raise CommandError("No merchant exists for historical attention demo orders.")

        now = timezone.now()
        created_count = 0
        updated_count = 0

        for old_reference, reference, days_old, customer_name, phone, amount in HISTORICAL_ORDERS:
            customer, _ = CustomerInfo.objects.update_or_create(
                customer_phone=phone,
                defaults={
                    "customer_name": customer_name,
                    "customer_email": f"order.{reference}@example.com",
                    "customer_address": "Historical attention queue demo order",
                },
            )
            existing_order = OrderInfo.objects.filter(
                merchant=merchant,
                merchant_order_id=old_reference,
            ).first()
            if existing_order and not OrderInfo.objects.filter(
                merchant=merchant,
                merchant_order_id=reference,
            ).exists():
                existing_order.merchant_order_id = reference
                existing_order.save(update_fields=["merchant_order_id"])
            order, created = OrderInfo.objects.update_or_create(
                merchant=merchant,
                merchant_order_id=reference,
                defaults={
                    "pa_order_id": f"PA{reference}",
                    "order_amount": Decimal(amount),
                    "order_currency": "INR",
                    "order_status": "pending",
                    "payment_state": "FUNDS_HELD",
                    "payment_state_updated_at": now - timedelta(days=days_old),
                    "payment_state_metadata": {
                        "seeded_for": "historical_attention_queue",
                        "days_old": days_old,
                    },
                    "customer_info": customer,
                    "shipment_id": None,
                    "payment_provider": "MockPay",
                },
            )
            OrderInfo.objects.filter(id=order.id).update(
                order_date=now - timedelta(days=days_old)
            )
            if created:
                created_count += 1
            else:
                updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded historical attention orders: "
                f"{created_count} created, {updated_count} updated."
            )
        )
