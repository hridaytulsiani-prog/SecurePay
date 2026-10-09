from datetime import datetime
import time
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand
from django.utils import timezone

from tracking.api.v1.courier_tracking_sync import normalize_courier, refresh_courier_snapshot
from tracking.api.v1.trackparcel import refresh_trackparcel_snapshot, should_refresh_snapshot
from tracking.models.tracking_snapshot import TrackingSnapshot


class Command(BaseCommand):
    help = "Refresh supported courier shipments using daily and delivery-day schedules."

    def add_arguments(self, parser):
        parser.add_argument(
            "--provider",
            choices=[
                "all", "delhivery", "dtdc", "shiprocket", "ekart", "shadowfax",
                "delhivery_public", "trackparcel",
            ],
            default="all",
        )
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument("--now", help="Optional ISO datetime for local testing, e.g. 2026-09-10T09:00:00+05:30")

    def handle(self, *args, **options):
        now = self._get_now(options.get("now"))
        provider = options["provider"]
        if provider == "delhivery_public":
            provider = "delhivery"
        queryset = (
            TrackingSnapshot.objects.select_related("shipment")
            .exclude(normalized_status__in=["DELIVERED", "CANCELLED", "RTO_DELIVERED", "FAILED_FINAL"])
            .order_by("next_check_after", "last_checked_at", "id")
        )

        checked = 0
        refreshed = 0
        skipped = 0
        last_provider_request = {}
        for snapshot in queryset[: options["limit"]]:
            if provider == "trackparcel":
                pass
            elif provider != "all" and provider not in (snapshot.courier or "").lower():
                continue
            if provider == "all" and not any(
                name in (snapshot.courier or "").lower()
                for name in ("delhivery", "dtdc", "shiprocket", "ekart", "shadowfax")
            ):
                continue

            checked += 1
            should_refresh, reason = should_refresh_snapshot(snapshot, now=now)
            if not should_refresh:
                skipped += 1
                self.stdout.write(f"skip {snapshot.awb}: {reason}")
                continue

            try:
                courier_key = provider if provider not in {"all", "trackparcel"} else normalize_courier(snapshot.courier)
                if courier_key:
                    elapsed = time.monotonic() - last_provider_request.get(courier_key, 0)
                    if elapsed < 12:
                        time.sleep(12 - elapsed)

                if provider == "trackparcel":
                    _, did_refresh, refresh_reason = refresh_trackparcel_snapshot(snapshot.shipment, now=now)
                else:
                    _, did_refresh, refresh_reason = refresh_courier_snapshot(
                        snapshot.shipment,
                        provider=None if provider == "all" else provider,
                        now=now,
                    )
                if courier_key:
                    last_provider_request[courier_key] = time.monotonic()
            except Exception as exc:
                skipped += 1
                self.stdout.write(self.style.ERROR(f"failed {snapshot.awb}: {exc}"))
                continue

            refreshed += 1 if did_refresh else 0
            skipped += 0 if did_refresh else 1
            self.stdout.write(f"{snapshot.awb}: {refresh_reason}")

        self.stdout.write(
            self.style.SUCCESS(
                f"provider={provider} checked={checked} refreshed={refreshed} skipped={skipped}"
            )
        )

    def _get_now(self, raw_value):
        if raw_value:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed)
            return timezone.localtime(parsed)

        return timezone.localtime(timezone.now(), ZoneInfo("Asia/Kolkata"))
