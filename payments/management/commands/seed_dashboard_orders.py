from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from payments.models import CustomerInfo, MerchantInfo, OrderInfo
from tracking.models.trackinginfo import Shipment


DEMO_ORDERS = [
    {
        "courier": "Blue Dart",
        "merchant_order_id": "SP-DEMO-BD-240918-001",
        "pa_order_id": "ORD240918785642",
        "pa_payment_id": "PAY_BD_9F3K72LQ",
        "awb": "78564219384",
        "customer_name": "Aarav Mehta",
        "customer_email": "aarav.mehta@example.com",
        "customer_phone": "9876501201",
        "customer_address": "B-1204, Orchid Woods, Goregaon East, Mumbai, Maharashtra 400063",
        "amount": "1899.00",
        "order_status": "PAID",
        "payment_state": "PAYMENT_CAPTURED",
        "delivery_status": "IN_TRANSIT",
    },
    {
        "courier": "Xpressbees",
        "merchant_order_id": "SP-DEMO-XB-240918-002",
        "pa_order_id": "ORD240918143256",
        "pa_payment_id": "PAY_XB_4G8P91ZT",
        "awb": "14325678901234",
        "customer_name": "Priya Nair",
        "customer_email": "priya.nair@example.com",
        "customer_phone": "9876501202",
        "customer_address": "Flat 804, Prestige Lakeside, Whitefield, Bengaluru, Karnataka 560066",
        "amount": "1249.00",
        "order_status": "PAID",
        "payment_state": "PAYMENT_CAPTURED",
        "delivery_status": "OUT_FOR_DELIVERY",
    },
    {
        "courier": "Delhivery",
        "merchant_order_id": "SP-DEMO-DL-240918-003",
        "pa_order_id": "ORD240918763521",
        "pa_payment_id": "PAY_DL_8H2N64KV",
        "awb": "DLV240918763521",
        "customer_name": "Rohit Sharma",
        "customer_email": "rohit.sharma@example.com",
        "customer_phone": "9876501203",
        "customer_address": "21 Green Park Extension, New Delhi, Delhi 110016",
        "amount": "899.00",
        "order_status": "PAID",
        "payment_state": "SETTLED",
        "delivery_status": "DELIVERED",
    },
    {
        "courier": "Shiprocket",
        "merchant_order_id": "SP-DEMO-SR-240918-004",
        "pa_order_id": "ORD240918958473",
        "pa_payment_id": "PAY_SR_2K7M85RX",
        "awb": "SRK9584736210",
        "customer_name": "Neha Kapoor",
        "customer_email": "neha.kapoor@example.com",
        "customer_phone": "9876501204",
        "customer_address": "A-304, Emerald Heights, Baner, Pune, Maharashtra 411045",
        "amount": "2199.00",
        "order_status": "PAID",
        "payment_state": "FUNDS_HELD",
        "delivery_status": "PICKED_UP",
    },
    {
        "courier": "DTDC",
        "merchant_order_id": "SP-DEMO-DT-240918-005",
        "pa_order_id": "ORD240918987654",
        "pa_payment_id": "PAY_DT_6B5V18NM",
        "awb": "D9876543210",
        "customer_name": "Ishita Rao",
        "customer_email": "ishita.rao@example.com",
        "customer_phone": "9876501205",
        "customer_address": "12 Lake View Road, Alwarpet, Chennai, Tamil Nadu 600018",
        "amount": "1549.00",
        "order_status": "PENDING",
        "payment_state": "PAYMENT_PENDING",
        "delivery_status": "AWAITING_PICKUP",
    },
    {
        "courier": "Shadowfax",
        "merchant_order_id": "SP-DEMO-SF-240918-006",
        "pa_order_id": "ORD240918724509",
        "pa_payment_id": "PAY_SF_3C9T47QA",
        "awb": "SFX7245091836",
        "customer_name": "Kabir Sethi",
        "customer_email": "kabir.sethi@example.com",
        "customer_phone": "9876501206",
        "customer_address": "Villa 18, Palm Meadows, Kondapur, Hyderabad, Telangana 500084",
        "amount": "749.00",
        "order_status": "PAID",
        "payment_state": "PAYMENT_CAPTURED",
        "delivery_status": "DELIVERED",
    },
    {
        "courier": "Ekart",
        "merchant_order_id": "SP-DEMO-EK-240918-007",
        "pa_order_id": "ORD240918287654",
        "pa_payment_id": "PAY_EK_5D1W63PL",
        "awb": "FMPC2876543210",
        "customer_name": "Meera Iyer",
        "customer_email": "meera.iyer@example.com",
        "customer_phone": "9876501207",
        "customer_address": "5C, Sunrise Apartments, Salt Lake Sector V, Kolkata, West Bengal 700091",
        "amount": "3299.00",
        "order_status": "REFUNDED",
        "payment_state": "REFUNDED",
        "delivery_status": "RETURNED",
    },
]

LEGACY_DEMO_ORDERS = [
    ("mock_hold_pa_order_SMOKE_EVIDENCE_URL", "mock_hold_pa_order_SMOKE_EVIDENCE_URL", "12.00", "PAID", "FUNDS_HELD"),
    ("mock_hold_pa_order_PA_1789461762200", "mock_hold_pa_order_PA_1789461762200", "1250.00", "PAID", "FUNDS_HELD"),
    ("mock_hold_pa_order_REFUND_1789459215489", "mock_hold_pa_order_REFUND_1789459215489", "1499.00", "REFUNDED", "REFUNDED"),
    ("mock_hold_pa_order_SMOKE_ADAPTER_RELEASE_2", "mock_hold_pa_order_SMOKE_ADAPTER_RELEASE_2", "11.00", "SETTLED", "SETTLED"),
    ("mock_hold_pa_order_SMOKE_ADAPTER_RELEASE", "mock_hold_pa_order_SMOKE_ADAPTER_RELEASE", "11.00", "SETTLED", "SETTLED"),
    ("mock_hold_pa_order_0be41f130f", "mock_hold_pa_order_0be41f130f", "1499.00", "REFUNDED", "REFUNDED"),
]


class Command(BaseCommand):
    help = "Seed seven realistic courier orders for the merchant dashboard."

    def add_arguments(self, parser):
        parser.add_argument("--merchant-email", help="Merchant email to seed orders for.")
        parser.add_argument("--merchant-key", help="Merchant key to seed orders for.")
        parser.add_argument(
            "--clear-existing",
            action="store_true",
            help="Delete existing orders for the selected merchant before seeding the seven demo rows.",
        )
        parser.add_argument(
            "--include-legacy",
            action="store_true",
            help="Also recreate older mock_hold dashboard rows used in earlier local demos.",
        )

    def handle(self, *args, **options):
        merchant = self._get_merchant(options)

        if options["clear_existing"]:
            OrderInfo.objects.filter(merchant=merchant).delete()

        created_count = 0
        updated_count = 0

        for item in DEMO_ORDERS:
            customer, _ = CustomerInfo.objects.update_or_create(
                customer_phone=item["customer_phone"],
                defaults={
                    "customer_name": item["customer_name"],
                    "customer_email": item["customer_email"],
                    "customer_address": item["customer_address"],
                },
            )

            shipment, _ = Shipment.objects.update_or_create(
                awb=item["awb"],
                defaults={
                    "courier": item["courier"],
                    "courier_partner": item["courier"],
                    "pa_order_id": item["pa_order_id"],
                    "invoice": f"INV-{item['merchant_order_id']}",
                    "status": item["delivery_status"],
                    "history": [
                        {
                            "ts": timezone.now().isoformat(),
                            "status": item["delivery_status"],
                            "note": f"Seeded {item['courier']} dashboard demo order.",
                        }
                    ],
                },
            )

            _, created = OrderInfo.objects.update_or_create(
                merchant=merchant,
                merchant_order_id=item["merchant_order_id"],
                defaults={
                    "pa_order_id": item["pa_order_id"],
                    "pa_payment_id": item["pa_payment_id"],
                    "phonepe_order_id": item["pa_order_id"],
                    "phonepe_payment_id": item["pa_payment_id"],
                    "order_amount": Decimal(item["amount"]),
                    "order_currency": "INR",
                    "order_status": item["order_status"],
                    "payment_state": item["payment_state"],
                    "payment_state_updated_at": timezone.now(),
                    "payment_state_metadata": {"seeded_for": "merchant_dashboard"},
                    "customer_info": customer,
                    "shipment_id": shipment,
                    "payment_provider": "MockPay",
                },
            )

            if created:
                created_count += 1
            else:
                updated_count += 1

        if options["include_legacy"]:
            legacy_created, legacy_updated = self._seed_legacy_orders(merchant)
            created_count += legacy_created
            updated_count += legacy_updated

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded dashboard orders for {merchant.merchant_email}: "
                f"{created_count} created, {updated_count} updated."
            )
        )

    def _get_merchant(self, options):
        if options["merchant_email"]:
            merchant = MerchantInfo.objects.filter(merchant_email=options["merchant_email"]).first()
            if not merchant:
                raise CommandError(f"No merchant found with email {options['merchant_email']}.")
            return merchant

        if options["merchant_key"]:
            merchant = MerchantInfo.objects.filter(merchant_key=options["merchant_key"]).first()
            if not merchant:
                raise CommandError(f"No merchant found with key {options['merchant_key']}.")
            return merchant

        merchant = MerchantInfo.objects.order_by("id").first()
        if not merchant:
            raise CommandError("No merchants exist. Create/login a merchant first, then rerun this command.")
        return merchant

    def _seed_legacy_orders(self, merchant):
        customer, _ = CustomerInfo.objects.update_or_create(
            customer_phone="9999999999",
            defaults={
                "customer_name": "Demo Customer",
                "customer_email": "demo.customer@example.com",
                "customer_address": "Demo address, Bengaluru, Karnataka 560001",
            },
        )

        created_count = 0
        updated_count = 0

        for merchant_order_id, pa_order_id, amount, order_status, payment_state in LEGACY_DEMO_ORDERS:
            _, created = OrderInfo.objects.update_or_create(
                merchant=merchant,
                merchant_order_id=merchant_order_id,
                defaults={
                    "pa_order_id": pa_order_id,
                    "pa_payment_id": f"PAY_{pa_order_id[-10:].upper()}",
                    "phonepe_order_id": pa_order_id,
                    "phonepe_payment_id": f"PAY_{pa_order_id[-10:].upper()}",
                    "order_amount": Decimal(amount),
                    "order_currency": "INR",
                    "order_status": order_status,
                    "payment_state": payment_state,
                    "payment_state_updated_at": timezone.now(),
                    "payment_state_metadata": {"seeded_for": "legacy_dashboard_demo"},
                    "customer_info": customer,
                    "shipment_id": None,
                    "payment_provider": "MockPay",
                },
            )

            if created:
                created_count += 1
            else:
                updated_count += 1

        return created_count, updated_count
