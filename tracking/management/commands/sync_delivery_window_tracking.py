from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand
from django.utils import timezone

from tracking.api.v1.delhivery_public_tracking import refresh_delhivery_public_snapshot
from tracking.api.v1.trackparcel import (
    DELIVERY_WINDOW_END_HOUR,
    DELIVERY_WINDOW_START_HOUR,
    refresh_trackparcel_snapshot,
    should_refresh_snapshot,
)
from tracking.models.tracking_snapshot import TrackingSnapshot


class Command(BaseCommand):
    help = "Refresh shipments automatically on their expected delivery date during the delivery window."

    def add_arguments(self, parser):
        parser.add_argument("--provider", choices=["delhivery_public", "trackparcel"], default="delhivery_public")
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument("--now", help="Optional ISO datetime for local testing, e.g. 2026-09-10T09:00:00+05:30")

    def handle(self, *args, **options):
        now = self._get_now(options.get("now"))
        if not (DELIVERY_WINDOW_START_HOUR <= now.hour < DELIVERY_WINDOW_END_HOUR):
            self.stdout.write(
                self.style.WARNING(
                    f"outside delivery window: {now.isoformat()} "
                    f"({DELIVERY_WINDOW_START_HOUR}:00-{DELIVERY_WINDOW_END_HOUR}:00)"
                )
            )
            return

        provider = options["provider"]
        today = now.date()
        queryset = (
            TrackingSnapshot.objects.select_related("shipment")
            .filter(expected_delivery_date=today)
            .exclude(normalized_status__in=["DELIVERED", "CANCELLED", "RTO_DELIVERED", "FAILED_FINAL"])
            .order_by("next_check_after", "last_checked_at", "id")
        )

        checked = 0
        refreshed = 0
        skipped = 0
        for snapshot in queryset[: options["limit"]]:
            checked += 1
            should_refresh, reason = should_refresh_snapshot(snapshot, now=now)
            if not should_refresh:
                skipped += 1
                self.stdout.write(f"skip {snapshot.awb}: {reason}")
                continue

            try:
                if provider == "trackparcel":
                    _, did_refresh, refresh_reason = refresh_trackparcel_snapshot(snapshot.shipment, now=now)
                else:
                    _, did_refresh, refresh_reason = refresh_delhivery_public_snapshot(snapshot.shipment, now=now)
            except Exception as exc:
                skipped += 1
                self.stdout.write(self.style.ERROR(f"failed {snapshot.awb}: {exc}"))
                continue

            refreshed += 1 if did_refresh else 0
            skipped += 0 if did_refresh else 1
            self.stdout.write(f"{snapshot.awb}: {refresh_reason}")

        self.stdout.write(
            self.style.SUCCESS(
                f"delivery_date={today} provider={provider} checked={checked} refreshed={refreshed} skipped={skipped}"
            )
        )

    def _get_now(self, raw_value):
        if raw_value:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed)
            return timezone.localtime(parsed)

        return timezone.localtime(timezone.now(), ZoneInfo("Asia/Kolkata"))
