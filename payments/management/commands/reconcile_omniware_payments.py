from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from payments.adapters.omniware import OmniwareError, get_payment_status
from payments.models import PaymentAttempt


class Command(BaseCommand):
    help = "Check pending Omniware attempts against the provider status API."

    def add_arguments(self, parser):
        parser.add_argument("--attempt-id", type=int)

    def handle(self, *args, **options):
        attempts = PaymentAttempt.objects.filter(
            provider="omniware",
            status__in=[PaymentAttempt.STATUS_CREATED, PaymentAttempt.STATUS_PENDING, PaymentAttempt.STATUS_UNKNOWN],
        )
        if options["attempt_id"]:
            attempts = attempts.filter(id=options["attempt_id"])
        if getattr(settings, "PAYMENT_PROVIDER", "") != "omniware":
            raise CommandError("Set PAYMENT_PROVIDER=omniware only after credentials are configured.")
        checked = 0
        for attempt in attempts.iterator():
            try:
                _request, response = get_payment_status(attempt.provider_order_id)
            except OmniwareError as exc:
                self.stderr.write(f"Attempt {attempt.id}: {exc}")
                continue
            attempt.response_payload = response
            attempt.save(update_fields=["response_payload", "updated_at"])
            checked += 1
            self.stdout.write(f"Attempt {attempt.id}: provider response saved")
        self.stdout.write(self.style.SUCCESS(f"Checked {checked} attempt(s)."))
