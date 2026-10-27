from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from adminpanel.merchant_risk import calculate_merchant_score
from payments.models import CustomerInfo, MerchantInfo, OrderInfo, SettlementRecord
from tracking.models import Shipment


SAMPLE_ORDER_PREFIX = "SP-RISK-SAMPLE-"
OBSOLETE_DEMO_MERCHANT_PREFIX = "sp_performance_demo_"
SAMPLE_COURIERS = ["Delhivery", "Blue Dart", "Xpressbees"]

TARGETS = [
    {
        "email": "green-row-test@escrosafe.local",
        "code": "GREEN",
        "states": ["SETTLED", "SETTLED", "SETTLED", "SETTLED", "SETTLED", "REFUNDED"],
    },
    {
        "email": "kunal11223344@gmail.com",
        "code": "KUNAL1",
        "states": ["SETTLED", "SETTLED", "SETTLED", "SETTLED", "REFUNDED"],
    },
    {
        "email": "kunalsingh541@gmail.com",
        "code": "KUNAL2",
        "states": ["SETTLED", "SETTLED", "SETTLED", "REFUNDED", "CANCELLED"],
    },
]


class Command(BaseCommand):
    help = "Add repeatable success-rate sample orders to selected existing merchants."

    @transaction.atomic
    def handle(self, *args, **options):
        missing = [
            target["email"]
            for target in TARGETS
            if not MerchantInfo.objects.filter(merchant_email=target["email"]).exists()
        ]
        if missing:
            raise CommandError(f"Selected merchant(s) not found: {', '.join(missing)}")

        obsolete = MerchantInfo.objects.filter(merchant_key__startswith=OBSOLETE_DEMO_MERCHANT_PREFIX)
        removed_names = list(obsolete.values_list("merchant_name", flat=True))
        obsolete.delete()
        if removed_names:
            self.stdout.write(f"Removed obsolete demo merchants: {', '.join(removed_names)}")
        Shipment.objects.filter(awb__startswith="SPPERF").delete()
        CustomerInfo.objects.filter(
            customer_email__startswith="performance.customer.",
            customer_email__endswith="@securepay.demo",
        ).delete()

        for target_index, target in enumerate(TARGETS, start=1):
            merchant = MerchantInfo.objects.get(merchant_email=target["email"])

            # Replace only sample rows created by this command; preserve all real orders.
            OrderInfo.objects.filter(
                merchant=merchant,
                merchant_order_id__startswith=SAMPLE_ORDER_PREFIX,
            ).delete()

            for order_index, outcome in enumerate(target["states"], start=1):
                token = f"{target_index:02d}{order_index:02d}"
                customer, _ = CustomerInfo.objects.update_or_create(
                    customer_phone=f"922200{token}",
                    defaults={
                        "customer_name": f"Sample Customer {token}",
                        "customer_email": f"merchant.sample.{token}@securepay.local",
                        "customer_address": "SecurePay merchant performance sample order",
                    },
                )

                if outcome == "SETTLED":
                    payment_state = "SETTLED"
                    order_status = "SETTLED"
                    delivery_status = "DELIVERED"
                elif outcome == "REFUNDED":
                    payment_state = "REFUNDED"
                    order_status = "REFUNDED"
                    delivery_status = "NOT_DELIVERED"
                else:
                    payment_state = "REFUNDED"
                    order_status = "CANCELLED"
                    delivery_status = "CANCELLED"

                shipment, _ = Shipment.objects.update_or_create(
                    awb=f"SPSAMPLE{token}",
                    defaults={
                        "courier": SAMPLE_COURIERS[(order_index - 1) % len(SAMPLE_COURIERS)],
                        "courier_partner": SAMPLE_COURIERS[(order_index - 1) % len(SAMPLE_COURIERS)],
                        "pa_order_id": f"SP-SAMPLE-PA-{token}",
                        "invoice": f"SP-SAMPLE-INV-{token}",
                        "status": delivery_status,
                        "history": [{
                            "ts": timezone.now().isoformat(),
                            "status": delivery_status,
                            "note": "Sample data for the merchant success-rate view.",
                        }],
                    },
                )

                amount = Decimal("1000.00") + Decimal(order_index * 100)
                order = OrderInfo.objects.create(
                    merchant=merchant,
                    merchant_order_id=f"{SAMPLE_ORDER_PREFIX}{target['code']}-{order_index:02d}",
                    pa_order_id=f"SP-SAMPLE-PA-{token}",
                    pa_payment_id=f"SP-SAMPLE-PAY-{token}",
                    order_amount=amount,
                    order_currency="INR",
                    order_status=order_status,
                    payment_state=payment_state,
                    payment_state_updated_at=timezone.now(),
                    payment_state_metadata={"seeded_for": "existing_merchant_performance"},
                    customer_info=customer,
                    shipment_id=shipment,
                    payment_provider="SamplePay",
                )

                if payment_state == "SETTLED":
                    SettlementRecord.objects.create(
                        order=order,
                        provider="SamplePay",
                        settlement_id=f"SP-SAMPLE-SETTLE-{token}",
                        status=SettlementRecord.STATUS_SETTLED,
                        gross_amount=amount,
                        fee_amount=Decimal("0.00"),
                        net_amount=amount,
                        eligible_at=timezone.now(),
                        settled_at=timezone.now(),
                    )

            profile = calculate_merchant_score(merchant)
            score = f"{profile.success_rate}%" if profile.scored_orders else "N/A"
            self.stdout.write(
                f"{merchant.merchant_name} ({merchant.merchant_email}): {score} | "
                f"orders={profile.total_orders}, delivered={profile.delivered_orders}, "
                f"settled={profile.successful_orders}, unsuccessful={profile.unsuccessful_orders}"
            )

        self.stdout.write(self.style.SUCCESS("Existing merchant performance data is ready."))
