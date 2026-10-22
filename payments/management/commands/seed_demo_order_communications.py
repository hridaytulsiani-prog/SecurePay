from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from adminpanel.models import (
    CustomerIssue,
    CustomerIssueMessage,
    CustomerIssueNote,
    MerchantIssue,
    MerchantIssueMessage,
    MerchantIssueNote,
)
from payments.models import OrderInfo


class Command(BaseCommand):
    help = "Seed persistent conversation history for ESC demo orders."

    def add_arguments(self, parser):
        parser.add_argument("--order-id", help="Seed one merchant order ID. Defaults to every ESC-* demo order.")

    def handle(self, *args, **options):
        orders = OrderInfo.objects.select_related("merchant", "customer_info", "shipment_id")
        if options.get("order_id"):
            orders = orders.filter(merchant_order_id=options["order_id"])
        else:
            orders = orders.filter(merchant_order_id__startswith="ESC-")

        orders = list(orders.order_by("order_date"))
        if not orders:
            raise CommandError("No matching demo orders were found.")

        for order in orders:
            self._seed_order(order)

        self.stdout.write(self.style.SUCCESS(f"Seeded communication history for {len(orders)} demo order(s)."))

    def _seed_order(self, order):
        shipment = order.shipment_id
        merchant = order.merchant
        customer = order.customer_info
        order_id = order.merchant_order_id
        awb = shipment.awb if shipment else "Not assigned"
        courier = shipment.courier if shipment else "Not assigned"
        delivery_status = shipment.status if shipment else "AWAITING_SHIPMENT"
        delivery_label = delivery_status.replace("_", " ").title()
        base_time = order.order_date or timezone.now()

        order_snapshot = {
            "order_id": order.id,
            "merchant_order_id": order_id,
            "awb": awb if shipment else "",
            "courier": courier if shipment else "",
            "delivery_status": delivery_status,
            "payment_state": order.payment_state,
        }
        merchant_description = (
            f"Please confirm the payment and shipment update for order {order_id}. "
            f"The courier record shows {delivery_label} for AWB {awb}."
        )
        merchant_issue, _ = MerchantIssue.objects.update_or_create(
            merchant_id=order.merchant_id,
            description=merchant_description,
            defaults={
                "merchant_name": merchant.merchant_name if merchant else "Demo Merchant",
                "merchant_email": merchant.merchant_email if merchant else "",
                "issue_type": "payment_delayed" if str(delivery_status).upper() == "DELIVERED" else "tracking_stuck",
                "orders": [order_snapshot],
                "status": MerchantIssue.STATUS_RESOLVED,
                "admin_response": "The order records were reviewed and the latest status was shared with the merchant.",
            },
        )
        merchant_messages = [
            (MerchantIssueMessage.SENDER_MERCHANT, merchant_description, timedelta(hours=2)),
            (MerchantIssueMessage.SENDER_ADMIN, "We have received your request and are checking the courier and payment records for this order.", timedelta(hours=3)),
            (MerchantIssueMessage.SENDER_MERCHANT, f"The latest courier update for AWB {awb} is visible on our dashboard. Please confirm the next payment step.", timedelta(hours=5)),
            (MerchantIssueMessage.SENDER_ADMIN, f"We verified the order history. The shipment status is {delivery_label}, and the payment will follow the applicable settlement workflow.", timedelta(hours=7)),
        ]
        self._seed_messages(merchant_issue, MerchantIssueMessage, merchant_messages, base_time)
        merchant_note, _ = MerchantIssueNote.objects.get_or_create(
            issue=merchant_issue,
            note="Courier, AWB, delivery, and payment records reviewed for this demo order.",
        )
        MerchantIssueNote.objects.filter(pk=merchant_note.pk).update(created_at=base_time + timedelta(hours=6))
        MerchantIssue.objects.filter(pk=merchant_issue.pk).update(created_at=base_time + timedelta(hours=2))

        customer_description = f"Please confirm the latest delivery update for order {order_id}."
        customer_issue, _ = CustomerIssue.objects.update_or_create(
            order_ref=order_id,
            description=customer_description,
            defaults={
                "customer_name": customer.customer_name if customer else "Demo Customer",
                "customer_phone": customer.customer_phone if customer else "",
                "customer_email": customer.customer_email if customer else "",
                "order": {
                    "merchant_order_id": order_id,
                    "awb": awb if shipment else "",
                    "courier": courier if shipment else "",
                    "merchant_name": merchant.merchant_name if merchant else "",
                    "amount": str(order.order_amount),
                    "delivery_status": delivery_status,
                    "payment_state": order.payment_state,
                },
                "issue_type": "delayed",
                "status": CustomerIssue.STATUS_RESOLVED,
            },
        )
        customer_messages = [
            (CustomerIssueMessage.SENDER_CUSTOMER, customer_description, timedelta(hours=1)),
            (CustomerIssueMessage.SENDER_ADMIN, f"We checked the courier record. The current shipment status is {delivery_label}.", timedelta(hours=4)),
            (CustomerIssueMessage.SENDER_CUSTOMER, "Thank you for confirming the update.", timedelta(hours=6)),
        ]
        self._seed_messages(customer_issue, CustomerIssueMessage, customer_messages, base_time)
        customer_note, _ = CustomerIssueNote.objects.get_or_create(
            issue=customer_issue,
            note="Customer was informed of the latest courier status for this demo order.",
        )
        CustomerIssueNote.objects.filter(pk=customer_note.pk).update(created_at=base_time + timedelta(hours=5))
        CustomerIssue.objects.filter(pk=customer_issue.pk).update(created_at=base_time + timedelta(hours=1))

    @staticmethod
    def _seed_messages(issue, model, messages, base_time):
        for sender, body, offset in messages:
            message, _ = model.objects.get_or_create(issue=issue, sender=sender, body=body)
            model.objects.filter(pk=message.pk).update(created_at=base_time + offset)
