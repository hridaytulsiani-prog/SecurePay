import csv
import time
from pathlib import Path

import requests
from django.core.management.base import BaseCommand, CommandError

from tracking.api.v1.delhivery_otp import (
    fetch_delhivery_tracking,
    normalize_awb,
    parse_delhivery_otp_status,
    stable_json_hash,
)
from tracking.models.delhivery_otp_check import DelhiveryOtpCheck


class Command(BaseCommand):
    help = "Safely batch-check Delhivery AWBs through the same OTP checker used by the admin UI."

    def add_arguments(self, parser):
        parser.add_argument("awb_file", type=str, help="Text file with one Delhivery AWB per line.")
        parser.add_argument(
            "--output",
            type=str,
            default="output/delhivery_otp_batch_results.csv",
            help="CSV output path. Default: output/delhivery_otp_batch_results.csv",
        )
        parser.add_argument(
            "--delay-seconds",
            type=float,
            default=12.0,
            help="Delay between requests. Default 12 seconds, about 5 requests/minute.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Maximum AWBs to check from the file. Default 50.",
        )
        parser.add_argument(
            "--no-store",
            action="store_true",
            help="Do not save DelhiveryOtpCheck audit rows in the database.",
        )

    def handle(self, *args, **options):
        awb_path = Path(options["awb_file"])
        if not awb_path.exists():
            raise CommandError(f"AWB file not found: {awb_path}")

        delay_seconds = options["delay_seconds"]
        if delay_seconds < 6:
            raise CommandError("delay-seconds must be at least 6 for the public Delhivery endpoint.")

        limit = options["limit"]
        awbs = []
        for line in awb_path.read_text(encoding="utf-8").splitlines():
            awb = normalize_awb(line)
            if awb and awb not in awbs:
                awbs.append(awb)
            if len(awbs) >= limit:
                break

        if not awbs:
            raise CommandError("No AWBs found in input file.")

        output_path = Path(options["output"])
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with output_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(
                csv_file,
                fieldnames=[
                    "awb",
                    "http_status",
                    "otp_status",
                    "delivery_status",
                    "evidence",
                    "response_hash",
                    "error",
                ],
            )
            writer.writeheader()

            for index, awb in enumerate(awbs, start=1):
                row = self._check_awb(awb, store=not options["no_store"])
                writer.writerow(row)
                csv_file.flush()

                self.stdout.write(
                    f"{index}/{len(awbs)} {awb}: {row['otp_status']} / {row['delivery_status']}"
                )

                if index < len(awbs):
                    time.sleep(delay_seconds)

        self.stdout.write(self.style.SUCCESS(f"Saved results to {output_path}"))

    def _check_awb(self, awb, store):
        check = None
        if store:
            check = DelhiveryOtpCheck.objects.create(awb=awb)

        try:
            http_status, payload = fetch_delhivery_tracking(awb)
            parsed = parse_delhivery_otp_status(payload)
            response_hash = stable_json_hash(payload)
            evidence = "; ".join(item.get("value", "") for item in parsed["evidence"][:3])

            if check:
                check.http_status = http_status
                check.raw_response = payload
                check.response_hash = response_hash
                check.otp_status = parsed["otp_status"]
                check.delivery_status = parsed["delivery_status"]
                check.evidence = parsed["evidence"]
                check.save(
                    update_fields=[
                        "http_status",
                        "raw_response",
                        "response_hash",
                        "otp_status",
                        "delivery_status",
                        "evidence",
                    ]
                )

            return {
                "awb": awb,
                "http_status": http_status,
                "otp_status": parsed["otp_status"],
                "delivery_status": parsed["delivery_status"],
                "evidence": evidence,
                "response_hash": response_hash,
                "error": "",
            }
        except (requests.RequestException, ValueError) as exc:
            if check:
                check.otp_status = DelhiveryOtpCheck.FETCH_FAILED
                check.error = str(exc)
                response = getattr(exc, "response", None)
                if response is not None:
                    check.http_status = response.status_code
                check.save(update_fields=["otp_status", "error", "http_status"])

            return {
                "awb": awb,
                "http_status": getattr(getattr(exc, "response", None), "status_code", ""),
                "otp_status": DelhiveryOtpCheck.FETCH_FAILED,
                "delivery_status": "",
                "evidence": "",
                "response_hash": "",
                "error": str(exc),
            }
