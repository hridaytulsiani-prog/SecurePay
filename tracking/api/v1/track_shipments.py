# Shipment lifecycle: create (single or bulk-via-CSV), poll/update status
# from the ShipSagar courier API, and fire a "you've been delivered, tell us
# if something's wrong" WhatsApp notification (which is what generates the
# EnquiryData rows the admin panel later reviews).
#
# *** SECURITY NOTE: none of the views in this file check merchant auth ***
# Unlike almost every other endpoint in this project (payments/api/v1's
# views, tracking.api.v1.enquiry_list, etc. — all of which call
# payments.auth.get_merchant_id_from_token or similar), CreateShipment,
# CreateBulkShipment, UpdateShipment, and TrackShipmentShipsagar below have
# NO authentication check at all. Any caller who can reach this Django
# instance can:
#   - create a shipment record and attach it to an arbitrary existing order
#     (by pa_order_id) — CreateShipment / CreateBulkShipment
#   - mark ANY shipment "delivered", which (via notify_customer_delivered)
#     sends a real WhatsApp message to a real customer and generates a real,
#     signed enquiry link for them — UpdateShipment
#   - trigger a ShipSagar status pull for any AWB — TrackShipmentShipsagar
# UpdateShipment additionally has `@method_decorator(csrf_exempt, ...)` with
# an explicit "# remove this in production" comment already in the code
# (see that class below) — that comment is correct and still unaddressed.
# None of this is fixed here (adding auth checks is a behavior change, not a
# comment), but if this project is going to production, gating these views
# behind get_merchant_id_from_token (like every other merchant endpoint
# does) should be high priority — an attacker sending fake "delivered"
# notifications to real customers is a meaningfully bad outcome, not just a
# data-integrity nuisance.
import io
import csv
from datetime import datetime

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views import View
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from tracking.models.trackinginfo import Shipment
from tracking.signing import build_enquiry_link
from tracking.api.v1.notifs import send_whatsapp_text
from tracking.api.v1.message_templates import delivery_enquiry_message
from tracking.api.v1.delhivery_public_tracking import (
    refresh_delhivery_public_snapshot,
    serialize_delhivery_public_snapshot,
)
from tracking.api.v1.trackparcel import get_or_create_snapshot, parse_date_value, refresh_trackparcel_snapshot
from payments.models import OrderInfo
from payments.auth import get_merchant_id_from_token
import json
import logging
import requests

logger = logging.getLogger(__name__)


def _get_auth_token_from_request(request):
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1].strip()
    return request.GET.get("auth_token")


def notify_customer_delivered(pa_order_id):
    """Send the customer a WhatsApp message with their signed order-enquiry link."""
    order_info = OrderInfo.objects.filter(pa_order_id=pa_order_id).select_related("customer_info").first()
    if not order_info or not order_info.customer_info or not order_info.customer_info.customer_phone:
        logger.warning("Skipping delivery notification: no customer phone for order %s", pa_order_id)
        return

    link = build_enquiry_link(settings.FRONTEND_ENQUIRY_URL, pa_order_id)
    message = delivery_enquiry_message(
        order_id=pa_order_id,
        enquiry_link=link,
        customer_name=order_info.customer_info.customer_name,
    )

    try:
        send_whatsapp_text(
            order_info.customer_info.customer_phone,
            message,
            preview_url=True,
            purpose="delivery_enquiry",
            order_id=pa_order_id,
        )
    except Exception:
        logger.exception("Failed to send delivery WhatsApp notification for order %s", pa_order_id)


@method_decorator(csrf_exempt, name="dispatch")
class CreateShipment(View):
    """Create one shipment and link it back onto its OrderInfo row, then
    forward it to ShipSagar (push_to_shipsagar) so the courier knows about it."""

    def post(self, request):
        # No auth check here — see the module-level security note at the top
        # of this file. Also note: no auth means this doesn't know which
        # merchant is calling, so `secureupi_order_id` is trusted at face
        # value; a caller could attach a new Shipment to ANY merchant's
        # order, not just their own.
        auth_token = _get_auth_token_from_request(request)
        token_merchant_id = get_merchant_id_from_token(auth_token)
        if token_merchant_id is None:
            return JsonResponse({"error": "Invalid or expired merchant token"}, status=401)

        request_data = json.loads(request.body.decode('utf-8'))
        try:
            order_info = OrderInfo.objects.filter(
                pa_order_id=request_data.get('secureupi_order_id'),
                merchant_id=token_merchant_id,
            ).first()
            if not order_info:
                return JsonResponse({'error': 'Order not found'}, status=404)

            shipment_data = {
                'courier': request_data.get('courier'),
                'pa_order_id': request_data.get('secureupi_order_id'),
            }
            if request_data.get('awb'):
                shipment_data['awb'] = request_data.get('awb')

            shipment = Shipment.objects.create(**shipment_data)
            # If no OrderInfo matches this pa_order_id, order_info is None
            # and the next line raises AttributeError — caught by the
            # `except Exception` below and returned as a 400. Not the most
            # descriptive error message for "order not found" specifically,
            # but functionally the request is correctly rejected either way.
            order_info.shipment_id = shipment
            order_info.save()
            # ShipSagar integration is preserved below, but TrackParcel is the
            # active provider for testing because billing is per API call.
            # push_to_shipsagar(shipment.awb, request_data.get('courier'), order_info.pa_order_id, order_info.customer_info.customer_name, order_info.customer_info.customer_email, order_info.customer_info.customer_phone)
            get_or_create_snapshot(shipment)
            print(f"Shipment created with AWB: {shipment.awb}, Courier: {shipment.courier}, PA Order ID: {shipment.pa_order_id}")
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
        return JsonResponse({'awb': shipment.awb})

    def get(self, request, awb=None):
        """
        GET may be called as:
         - /tracking/track_shipment/?awb=AWB123
         - /tracking/track_shipment/AWB123/
        """
        # accept awb from path param first, then from query string
        if not awb:
            awb = request.GET.get('awb')

        if not awb:
            return JsonResponse({'error': 'AWB is required either as path param or ?awb='}, status=400)

        try:
            shipment = Shipment.objects.get(awb=awb)
            shipment_data = {
                'awb': shipment.awb,
                'courier': shipment.courier,
                'pa_order_id': shipment.pa_order_id,
                'status': shipment.status,
                'history': shipment.history,
                'created_at': shipment.created_at.isoformat()
            }
            print(f"Retrieved shipment: {shipment_data}")
        except Shipment.DoesNotExist:
            return JsonResponse({'error': 'Shipment not found'}, status=404)

        return JsonResponse(shipment_data)

# tracking/views.py
import uuid
from django.http import JsonResponse
from django.views import View
from django.db import transaction
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from tracking.models.trackinginfo import Shipment

@method_decorator(csrf_exempt, name="dispatch")   # remove this in production
# ^ this pre-existing comment is correct and still unaddressed: csrf_exempt
# means any website a merchant/admin happens to have open in another tab
# could POST to this endpoint on the visitor's behalf (classic CSRF) with no
# browser same-origin protection stopping it. Combined with there being NO
# auth check either (see this file's top-of-file security note), this
# endpoint — which marks a shipment delivered and fires a real customer
# WhatsApp notification — is currently reachable by literally any POST
# request from anywhere. Properly production-hardening this view means BOTH
# removing csrf_exempt (or replacing it with real token-based auth that
# doesn't need Django's cookie-based CSRF protection at all, like every
# other endpoint in this project already uses) AND adding a merchant/admin
# auth check.
class UpdateShipment(View):
    """
    POST /tracking/track_shipment/<awb>/delivered
    (also supports GET for convenience)
    """

    def post(self, request, awb=None, *args, **kwargs):
        return self._mark_delivered(request, awb)

    def get(self, request, awb=None, *args, **kwargs):
        # WARNING: GET should not be used to change state in real apps,
        # this is provided only for convenience/testing.
        return self._mark_delivered(request, awb)

    def _mark_delivered(self, request, awb):
        # accept awb from path or querystring
        if not awb:
            awb = request.GET.get("awb")

        if not awb:
            return JsonResponse({"ok": False, "error": "AWB is required (path param or ?awb=)"}, status=400)

        try:
            with transaction.atomic():
                # select_for_update + the already-DELIVERED short-circuit below
                # make this idempotent — calling it twice won't double-send
                # the delivery notification.
                shipment = Shipment.objects.select_for_update().get(awb=awb)
                if shipment.status == "DELIVERED":
                    return JsonResponse({"ok": True, "msg": "already delivered", "awb": awb, "status": shipment.status})

                shipment.status = "DELIVERED"
                # add history entry if model supports it
                try:
                    shipment.add_history("DELIVERED", note="Marked as delivered via API")
                except Exception:
                    # fallback if no helper method
                    hist = getattr(shipment, "history", []) or []
                    hist.append({"ts": str(uuid.uuid4()), "status": "DELIVERED", "note": "Marked delivered via API"})
                    try:
                        shipment.history = hist
                    except Exception:
                        pass

                shipment.save()
        except Shipment.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Shipment not found"}, status=404)
        except Exception as e:
            return JsonResponse({"ok": False, "error": str(e)}, status=500)

        notify_customer_delivered(shipment.pa_order_id)

        return JsonResponse({"ok": True, "awb": awb, "status": "DELIVERED"})

@method_decorator(csrf_exempt, name="dispatch")
class CreateBulkShipment(View):
    """CSV upload version of CreateShipment — one row per shipment, reported
    back per-row as success/failure rather than failing the whole batch."""

    def post(self, request):
        """
        POST /tracking/create_shipments_bulk/

        NOTE: this docstring's "Body: JSON array" description below is
        stale/wrong — the actual implementation expects a multipart file
        upload (a CSV, read via request.FILES), not a JSON array. See the
        `required_fields` set further down for the actual expected CSV
        columns: secureupi_order_id, awb, courier.
        Body: JSON array of shipments, each with 'courier', 'awb', 'secureupi_order_id'
        """
        auth_token = _get_auth_token_from_request(request)
        token_merchant_id = get_merchant_id_from_token(auth_token)
        if token_merchant_id is None:
            return JsonResponse({"error": "Invalid or expired merchant token"}, status=401)

        uploaded_file = request.FILES.get("file")

        if not uploaded_file:
            return JsonResponse(
                {
                    "error": "CSV file is required"
                },
                status=400
            )

        # --------------------------------------------------
        # Validate file
        # --------------------------------------------------

        if not uploaded_file.name.lower().endswith(".csv"):
            return JsonResponse(
                {
                    "error": "Only CSV files are allowed"
                },
                status=400
            )

        try:

            content = uploaded_file.read().decode("utf-8-sig")

        except UnicodeDecodeError:

            return JsonResponse(
                {
                    "error": "CSV must be UTF-8 encoded"
                },
                status=400
            )

        # --------------------------------------------------
        # Parse CSV
        # --------------------------------------------------

        try:

            reader = csv.DictReader(
                io.StringIO(content)
            )

            if not reader.fieldnames:

                return JsonResponse(
                    {
                        "error": "CSV has no header"
                    },
                    status=400
                )

            # Normalize headers
            fieldnames = [
                field.strip().lower()
                for field in reader.fieldnames
            ]

            required_fields = {
                "secureupi_order_id",
                "awb",
                "courier"
            }

            missing = required_fields - set(fieldnames)

            if missing:

                return JsonResponse(
                    {
                        "error": "Missing CSV columns",
                        "missing": list(missing),
                        "required": [
                            "secureupi_order_id",
                            "awb",
                            "courier"
                        ]
                    },
                    status=400
                )

        except Exception as e:

            return JsonResponse(
                {
                    "error": f"Invalid CSV: {str(e)}"
                },
                status=400
            )

        # --------------------------------------------------
        # Process rows
        # --------------------------------------------------

        results = []

        success_count = 0
        failed_count = 0

        for row_number, row in enumerate(reader, start=2):

            # Normalize row keys
            normalized_row = {
                key.strip().lower(): (
                    value.strip()
                    if value
                    else ""
                )
                for key, value in row.items()
                if key
            }

            order_id = normalized_row.get(
                "secureupi_order_id",
                ""
            )

            awb = normalized_row.get(
                "awb",
                ""
            )

            courier = normalized_row.get(
                "courier",
                ""
            )

            # ----------------------------------------------
            # Validate row
            # ----------------------------------------------

            if not order_id:

                results.append({
                    "row": row_number,
                    "secureupi_order_id": "",
                    "success": False,
                    "error": "secureupi_order_id is required"
                })

                failed_count += 1
                continue

            if not courier:

                results.append({
                    "row": row_number,
                    "secureupi_order_id": order_id,
                    "success": False,
                    "error": "courier is required"
                })

                failed_count += 1
                continue

            # ----------------------------------------------
            # Create shipment
            # ----------------------------------------------

            try:
                order_info = OrderInfo.objects.filter(
                    pa_order_id=order_id,
                    merchant_id=token_merchant_id,
                ).first()
                if not order_info:
                    results.append({
                        "row": row_number,
                        "secureupi_order_id": order_id,
                        "success": False,
                        "error": "Order not found"
                    })
                    failed_count += 1
                    continue

                shipment_data = {
                    "courier": courier,
                    "pa_order_id": order_id
                }
                if awb:
                    shipment_data["awb"] = awb

                result = Shipment.objects.create(**shipment_data)
                order_info.shipment_id = result
                order_info.save()
                print(f"Shipment created with AWB: {result.awb}, Courier: {result.courier}, PA Order ID: {result.pa_order_id}")
                # ShipSagar integration is preserved below, but TrackParcel is
                # the active provider for testing.
                # push_to_shipsagar(result.awb, courier, order_info.pa_order_id, order_info.customer_info.customer_name, order_info.customer_info.customer_email, order_info.customer_info.customer_phone)
                get_or_create_snapshot(result)
                results.append({
                    "row": row_number,
                    "secureupi_order_id": order_id,
                    "success": True,
                    "awb": result.awb,
                    "status": result.status
                })

                success_count += 1

            except Exception as e:

                results.append({
                    "row": row_number,
                    "secureupi_order_id": order_id,
                    "success": False,
                    "error": str(e)
                })

                failed_count += 1

        # --------------------------------------------------
        # Response
        # --------------------------------------------------

        return JsonResponse({
            "success": True,
            "total": success_count + failed_count,
            "success_count": success_count,
            "failed_count": failed_count,
            "results": results
        })

def push_to_shipsagar(awb, courier, order_number, customer_name, customer_email, customer_phone):
    """
    Registers a shipment with the ShipSagar courier-aggregator API
    (app.shipsagar.com) so it shows up there for tracking — called right
    after a Shipment row is created (both single via CreateShipment and
    bulk-CSV via CreateBulkShipment call this).

    SETUP / CREDENTIALS: Token and ClientCode below are this SecurePay
    account's ShipSagar login, hardcoded directly in this function rather
    than read from settings/env vars (unlike every other external
    integration in this project — Razorpay, PhonePe, GetGabs — which all go
    through settings.py or os.getenv). If these ever need rotating, or if
    you're setting up a fresh ShipSagar account for a different deployment,
    this is the ONLY place to change them; there's no environment variable
    to override. Get replacement values from the ShipSagar merchant portal
    if you ever need to change accounts.

    THINGS TO KNOW BEFORE RELYING ON THIS:
      - The endpoint URL is plain http://, not https:// — the ClientCode/
        Token (and the customer's name/email/phone) travel over an
        unencrypted connection. This is ShipSagar's own API surface, not
        something this codebase controls, but it's worth knowing if you're
        ever asked "is this PII handled securely end-to-end".
      - "ShipmentType": "Train" is hardcoded for every shipment regardless
        of what the courier or package actually is — looks like a
        placeholder value that was never made shipment-specific.
      - "CompanyName": "XYZ Pvt Ltd" is also a hardcoded placeholder, not
        this business's actual registered name — likely needs updating with
        the real company name for ShipSagar's records to be accurate.
      - This function ALWAYS returns True and never raises, even when the
        HTTP request to ShipSagar fails outright or ShipSagar responds with
        an error — see the print() below, which is the only place a failure
        would be visible (and only if someone is watching server stdout in
        real time; nothing here uses the `logger` this module otherwise
        uses, and nothing is persisted). Practically: a caller checking
        `if push_to_shipsagar(...):` to decide whether the push worked will
        always think it succeeded. If ShipSagar delivery needs to be
        provably tracked, treat this the way WhatsApp sends were upgraded to
        use WhatsAppMessageLog (see tracking.api.v1.notifs) — persisting a
        log row with the actual response/status — rather than relying on
        stdout.
    """
    url = 'http://app.shipsagar.com/api/Web/PushShipment'

    payload = {
      "Token": "0691F3D5-0B37-4520-A1DC-1DD4C151CC42",
      "ClientCode": "C1375",
      "CourierCode": courier,
      "TrackingNo": awb,
      "OrderNo": order_number,
      "CustomerName": customer_name,
      "EmailID": customer_email or 'random@gmail.com',
      "ShipmentType": "Train",
      "MobileNo": customer_phone,
      "CountryName": "India",
      "CompanyName": "XYZ Pvt Ltd",
    }
    response = requests.post(url, json=payload)
    print(f"Shipsagar response: {response.status_code} - {response.text}")
    print(f"Pushing shipment to Shipsagar: {payload}")
    return True

class TrackShipmentShipsagar(View):
    """Pulls the latest status from ShipSagar and mirrors it onto the local
    Shipment row; fires the delivery notification on the FIRST transition
    into 'delivered' only (was_delivered guards against re-notifying)."""

    def get(self, request, awb=None):
        """
        GET /tracking/track_shipment/shipsagar/<awb>/

        Token/ClientCode here are the same hardcoded ShipSagar account
        credentials as push_to_shipsagar() above — see that function's
        docstring for where to change them if they're ever rotated (there's
        no shared constant; both copies would need updating together).
        """
        url = 'http://app.shipsagar.com/api/Web/TrackShipment'
        payload = {
            "Token":"0691F3D5-0B37-4520-A1DC-1DD4C151CC42",
            "ClientCode":"C1375",
            "TrackingNo":awb
        }
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            response_data = response.json()
            shipment = Shipment.objects.filter(awb=awb).first()
            if not shipment:
                return JsonResponse({"error": "Shipment not found in local database"}, status=404)
            if response_data.get('status') != 'success':
                return JsonResponse({"error": "Failed to fetch shipment status from Shipsagar", "details": response_data}, status=400)
            # ShipSagar double-encodes this field — it's a JSON STRING
            # containing another JSON string containing the actual object —
            # hence parsing it twice. This isn't a typo; ShipSagar's API
            # really does nest it this way.
            tracking_details = json.loads(
                json.loads(response_data["trackingDetails"])
            )
            status = tracking_details.get("CurrentStatus", "")
            was_delivered = shipment.status.strip().lower() == "delivered"
            # NOTE: writes shipment.status directly rather than going through
            # Shipment.add_history() (see tracking/models/trackinginfo.py) —
            # so this status change does NOT get recorded in the shipment's
            # `history` audit trail, unlike UpdateShipment._mark_delivered()
            # elsewhere in this file, which does use add_history(). An
            # inconsistency worth knowing about if you're ever debugging why
            # a shipment's history looks incomplete.
            shipment.status = status
            shipment.save()

            if status.strip().lower() == "delivered" and not was_delivered:
                notify_customer_delivered(shipment.pa_order_id)
            return JsonResponse(status=200, data={
                "awb": shipment.awb,
                "secureupi_order_id": getattr(shipment, "pa_order_id", None) or getattr(shipment, "secureupi_order_id", None),
                "payment_id": getattr(shipment, "payment_id", None) or (getattr(shipment, "payment", None) and getattr(shipment.payment, "payment_id", None)),
                "courier": shipment.courier,
                "status": shipment.status,
                "history": shipment.history if hasattr(shipment, "history") else [],
                # NOTE: Shipment has no `secureupi_order`/`payment` attribute
                # (see the model — only awb/courier/pa_order_id/invoice/
                # status/history/created_at exist), so every getattr(shipment,
                # "secureupi_order", ...) / "payment" below always falls
                # through to None. In practice `order_status` and `customer`
                # in this response are ALWAYS null — this looks like it was
                # written against a richer Shipment model that this project
                # doesn't actually have. If you need order/customer info in
                # this response, look it up explicitly via
                # OrderInfo.objects.filter(shipment_id=shipment) instead.
                "order_status": getattr(shipment.secureupi_order, "status", None) if getattr(shipment, "secureupi_order", None) else None,
                "customer": {
                    "name": getattr(shipment.secureupi_order, "customer_name", None),
                    "phone": getattr(shipment.secureupi_order, "customer_phone", None),
                } if getattr(shipment, "secureupi_order", None) else None,
                "created_at": shipment.created_at.isoformat() if hasattr(shipment, "created_at") else None,
            })
        else:
            return JsonResponse({"error": "Failed to fetch shipment status from Shipsagar"}, status=response.status_code)


class TrackShipmentTrackParcel(View):
    """Default tracking view for testing TrackParcel without removing ShipSagar."""

    def get(self, request, awb=None):
        if not awb:
            awb = request.GET.get("awb")
        if not awb:
            return JsonResponse({"error": "AWB is required"}, status=400)

        force = str(request.GET.get("force", "")).lower() in {"1", "true", "yes"}
        shipment = Shipment.objects.filter(awb=awb).first()
        if not shipment:
            return JsonResponse({"error": "Shipment not found in local database"}, status=404)

        try:
            snapshot, refreshed, reason = refresh_trackparcel_snapshot(shipment, force=force)
        except RuntimeError as exc:
            return JsonResponse(
                {
                    "error": str(exc),
                    "provider": "trackparcel",
                    "hint": "Set TRACKPARCEL_API_KEY and confirm TRACKPARCEL_BASE_URL/TRACKPARCEL_TRACK_ENDPOINT.",
                },
                status=400,
            )
        except requests.RequestException as exc:
            upstream_response = getattr(exc, "response", None)
            upstream_status = getattr(upstream_response, "status_code", None)
            upstream_body = getattr(upstream_response, "text", "")
            return JsonResponse(
                {
                    "error": "Failed to fetch shipment status from TrackParcel",
                    "details": str(exc),
                    "upstream_status": upstream_status,
                    "upstream_response": upstream_body[:2000],
                    "provider": "trackparcel",
                },
                status=502,
            )

        return JsonResponse(
            status=200,
            data={
                "awb": shipment.awb,
                "secureupi_order_id": shipment.pa_order_id,
                "courier": shipment.courier,
                "status": snapshot.current_status or shipment.status,
                "normalized_status": snapshot.normalized_status,
                "history": shipment.history if hasattr(shipment, "history") else [],
                "provider": "trackparcel",
                "refreshed": refreshed,
                "refresh_reason": reason,
                "last_checked_at": snapshot.last_checked_at.isoformat() if snapshot.last_checked_at else None,
                "next_check_after": snapshot.next_check_after.isoformat() if snapshot.next_check_after else None,
                "expected_delivery_date": snapshot.expected_delivery_date.isoformat() if snapshot.expected_delivery_date else None,
                "delivered_at": snapshot.delivered_at.isoformat() if snapshot.delivered_at else None,
                "tracking": snapshot.raw_response,
                "created_at": shipment.created_at.isoformat() if hasattr(shipment, "created_at") else None,
            },
        )


class TrackShipmentDelhiveryPublic(View):
    """Temporary provider for testing cache/freshness logic without TrackParcel key."""

    def get(self, request, awb=None):
        if not awb:
            awb = request.GET.get("awb")
        if not awb:
            return JsonResponse({"error": "AWB is required"}, status=400)

        force = str(request.GET.get("force", "")).lower() in {"1", "true", "yes"}
        shipment = Shipment.objects.filter(awb=awb).first()
        if not shipment:
            return JsonResponse({"error": "Shipment not found in local database"}, status=404)

        try:
            snapshot, refreshed, reason = refresh_delhivery_public_snapshot(shipment, force=force)
        except requests.RequestException as exc:
            return JsonResponse(
                {
                    "error": "Failed to fetch shipment status from Delhivery public tracking",
                    "details": str(exc),
                    "provider": "delhivery_public",
                },
                status=502,
            )

        return JsonResponse(
            status=200,
            data=serialize_delhivery_public_snapshot(shipment, snapshot, refreshed, reason),
        )


@method_decorator(csrf_exempt, name="dispatch")
class TrackingSnapshotTestSetup(View):
    """Local test helper for simulating EDD/window freshness behavior."""

    def post(self, request, awb=None):
        if not settings.DEBUG:
            return JsonResponse({"error": "Test setup is available only when DEBUG=True"}, status=404)

        if not awb:
            awb = request.GET.get("awb")
        if not awb:
            return JsonResponse({"error": "AWB is required"}, status=400)

        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except ValueError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        shipment = Shipment.objects.filter(awb=awb).first()
        if not shipment:
            return JsonResponse({"error": "Shipment not found in local database"}, status=404)

        snapshot = get_or_create_snapshot(shipment)
        expected_delivery_date = parse_date_value(payload.get("expected_delivery_date"))
        if expected_delivery_date:
            snapshot.expected_delivery_date = expected_delivery_date

        if "last_checked_at" in payload:
            snapshot.last_checked_at = _parse_test_datetime(payload.get("last_checked_at"))

        if "next_check_after" in payload:
            snapshot.next_check_after = _parse_test_datetime(payload.get("next_check_after"))
        elif payload.get("clear_next_check_after", True):
            snapshot.next_check_after = None

        if payload.get("status"):
            snapshot.current_status = str(payload.get("status"))
            snapshot.normalized_status = str(payload.get("normalized_status") or payload.get("status")).upper()
            shipment.status = str(payload.get("status"))
            shipment.save(update_fields=["status"])

        snapshot.updated_at = timezone.now()
        snapshot.save()

        return JsonResponse(
            {
                "ok": True,
                "awb": shipment.awb,
                "expected_delivery_date": snapshot.expected_delivery_date.isoformat()
                if snapshot.expected_delivery_date
                else None,
                "last_checked_at": snapshot.last_checked_at.isoformat() if snapshot.last_checked_at else None,
                "next_check_after": snapshot.next_check_after.isoformat() if snapshot.next_check_after else None,
                "status": snapshot.current_status,
                "normalized_status": snapshot.normalized_status,
            }
        )


def _parse_test_datetime(value):
    if not value:
        return None
    if value == "now":
        return timezone.now()
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed
