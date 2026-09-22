from django.core.management.base import BaseCommand

from payments.models import DecisionHistory, EnquiryData
from payments.services.decision_history import add_decision_history


class Command(BaseCommand):
    help = "Backfill initial decision-history rows for enquiries that do not have a timeline yet."

    def handle(self, *args, **options):
        created = []
        for enquiry in EnquiryData.objects.order_by("id"):
            if DecisionHistory.objects.filter(case_type="enquiry", case_id=enquiry.enquiry_id).exists():
                continue

            add_decision_history(
                case_type="enquiry",
                case_id=enquiry.enquiry_id,
                order_id=enquiry.order_id,
                status="created",
                title="Enquiry Created",
                remarks="Enquiry case created.",
                actor_display="customer",
                actor_role="Customer",
                metadata={"receipt_status": enquiry.receipt_status},
            )
            add_decision_history(
                case_type="enquiry",
                case_id=enquiry.enquiry_id,
                order_id=enquiry.order_id,
                status="submitted",
                title="Submitted For Review",
                remarks=enquiry.enquiry_text,
                actor_display="customer",
                actor_role="Customer",
                metadata={
                    "receipt_status": enquiry.receipt_status,
                    "agent_contacted": enquiry.agent_contacted,
                    "otp_shared": enquiry.otp_shared,
                    "unboxing_evidence": enquiry.unboxing_evidence,
                },
            )

            if enquiry.resolution_status != "unresolved":
                add_decision_history(
                    case_type="enquiry",
                    case_id=enquiry.enquiry_id,
                    order_id=enquiry.order_id,
                    status=enquiry.resolution_status,
                    title="Current Decision Recorded",
                    remarks=enquiry.resolution_reason or "",
                    actor_display=enquiry.resolved_by.username if enquiry.resolved_by else "admin",
                    actor_role="Admin",
                    metadata={"enquiry_status": enquiry.status},
                )
            created.append(enquiry.enquiry_id)

        self.stdout.write(self.style.SUCCESS(f"Backfilled {len(created)} enquiries."))
