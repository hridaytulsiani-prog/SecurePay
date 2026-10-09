from django.core.management.base import BaseCommand
from django.utils import timezone

from tracking.api.v1.trackparcel import refresh_trackparcel_snapshot, should_refresh_snapshot
from tracking.models.tracking_snapshot import TrackingSnapshot


class Command(BaseCommand):
    help = "Refresh TrackParcel statuses only for shipments whose snapshot is stale."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **options):
        limit = options["limit"]
        force = options["force"]
        now = timezone.localtime()
        checked = 0
        refreshed = 0
        skipped = 0

        queryset = (
            TrackingSnapshot.objects.select_related("shipment")
            .exclude(normalized_status__in=["DELIVERED", "CANCELLED", "RTO_DELIVERED", "FAILED_FINAL"])
            .order_by("next_check_after", "last_checked_at", "id")
        )

        for snapshot in queryset[:limit]:
            checked += 1
            should_refresh, reason = should_refresh_snapshot(snapshot, force=force, now=now)
            if not should_refresh:
                skipped += 1
                self.stdout.write(f"skip {snapshot.awb}: {reason}")
                continue

            try:
                _, did_refresh, refresh_reason = refresh_trackparcel_snapshot(snapshot.shipment, force=force)
            except Exception as exc:
                skipped += 1
                self.stdout.write(self.style.ERROR(f"failed {snapshot.awb}: {exc}"))
                continue

            if did_refresh:
                refreshed += 1
            else:
                skipped += 1
            self.stdout.write(f"{snapshot.awb}: {refresh_reason}")

        self.stdout.write(
            self.style.SUCCESS(
                f"checked={checked} refreshed={refreshed} skipped={skipped}"
            )
        )
