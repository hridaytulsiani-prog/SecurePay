from django.contrib import admin

from tracking.models.pdf_validation import PdfValidationRecord
from tracking.models.whatsapp_log import WhatsAppMessageLog
from tracking.models.delhivery_otp_check import DelhiveryOtpCheck
from tracking.models.delhivery_verification_decision import DelhiveryVerificationDecision
from tracking.models.tracking_snapshot import TrackingApiCallLog, TrackingSnapshot


@admin.register(PdfValidationRecord)
class PdfValidationRecordAdmin(admin.ModelAdmin):
    list_display = ("file_name", "status", "verdict", "risk_verdict", "awb", "order_id", "uploaded_at")
    list_filter = ("status", "verdict", "risk_verdict", "delivery_partner", "courier_partner")
    search_fields = ("file_name", "awb", "order_id")
    readonly_fields = (
        "merchant",
        "file_name",
        "status",
        "verdict",
        "risk_verdict",
        "score",
        "risk_score",
        "delivery_partner",
        "courier_partner",
        "awb",
        "order_id",
        "details",
        "uploaded_at",
    )


@admin.register(WhatsAppMessageLog)
class WhatsAppMessageLogAdmin(admin.ModelAdmin):
    list_display = ("to_number", "purpose", "order_id", "status", "wa_message_id", "created_at")
    list_filter = ("status", "purpose")
    search_fields = ("to_number", "order_id", "wa_message_id", "message_body")
    readonly_fields = (
        "to_number",
        "purpose",
        "order_id",
        "message_body",
        "status",
        "error",
        "wa_id",
        "wa_message_id",
        "raw_response",
        "created_at",
        "updated_at",
    )


@admin.register(DelhiveryOtpCheck)
class DelhiveryOtpCheckAdmin(admin.ModelAdmin):
    list_display = ("awb", "otp_status", "delivery_status", "http_status", "checked_by", "checked_at")
    list_filter = ("otp_status", "delivery_status", "http_status")
    search_fields = ("awb", "response_hash")
    readonly_fields = (
        "awb",
        "courier",
        "otp_status",
        "delivery_status",
        "evidence",
        "raw_response",
        "response_hash",
        "http_status",
        "error",
        "checked_by",
        "checked_at",
    )


@admin.register(DelhiveryVerificationDecision)
class DelhiveryVerificationDecisionAdmin(admin.ModelAdmin):
    list_display = ("awb", "decision", "source_check", "decided_by", "decided_at")
    list_filter = ("decision",)
    search_fields = ("awb",)
    readonly_fields = ("awb", "decision", "source_check", "decided_by", "decided_at")


@admin.register(TrackingSnapshot)
class TrackingSnapshotAdmin(admin.ModelAdmin):
    list_display = ("awb", "courier", "source", "normalized_status", "last_checked_at", "next_check_after")
    list_filter = ("source", "normalized_status")
    search_fields = ("awb", "courier")
    readonly_fields = (
        "shipment",
        "awb",
        "courier",
        "source",
        "current_status",
        "normalized_status",
        "expected_delivery_date",
        "delivered_at",
        "last_checked_at",
        "next_check_after",
        "raw_response",
        "error",
        "updated_at",
    )


@admin.register(TrackingApiCallLog)
class TrackingApiCallLogAdmin(admin.ModelAdmin):
    list_display = ("provider", "awb", "courier", "http_status", "success", "called_at")
    list_filter = ("provider", "success", "http_status")
    search_fields = ("awb", "courier", "endpoint")
    readonly_fields = (
        "provider",
        "shipment",
        "awb",
        "courier",
        "endpoint",
        "http_status",
        "success",
        "request_payload",
        "response_payload",
        "response_headers",
        "error",
        "called_at",
    )
