# tracking/urls.py
# Mounted at /tracking/ in securepay/urls.py. Covers shipment creation/status
# (ShipSagar courier integration), the shipping-label PDF validator, and the
# customer-facing non-delivery enquiry form + its merchant-facing list.
from django.urls import path
from tracking.api.v1.track_shipments import (
    CreateBulkShipment,
    CreateShipment,
    TrackShipmentDelhiveryPublic,
    TrackShipmentShipsagar,
    TrackShipmentTrackParcel,
    TrackingSnapshotTestSetup,
    UpdateShipment,
)
from tracking.api.v1.validate import PDFValidator
from tracking.api.v1.enquiry import SubmitEnquiry
from tracking.api.v1.enquiry_list import EnquiryListView

urlpatterns = [
    path('create_shipment/', CreateShipment.as_view(), name='create_shipment'),
    path('create_shipments_bulk/', CreateBulkShipment.as_view(), name='create_shipments_bulk'),
    path('track_shipment/test-setup/<str:awb>/', TrackingSnapshotTestSetup.as_view(), name='tracking_snapshot_test_setup'),
    path('track_shipment/delhivery-public/<str:awb>/', TrackShipmentDelhiveryPublic.as_view(), name='track_shipment_delhivery_public'),
    path('track_shipment/shipsagar/<str:awb>/', TrackShipmentShipsagar.as_view(), name='track_shipment_shipsagar'),  # for ?awb= query param
    path('track_shipment/<str:awb>/', TrackShipmentTrackParcel.as_view(), name='track_shipment'),
    path('track_shipment/<str:awb>/delivered', UpdateShipment.as_view(), name='track_shipment_query'),  # for ?awb= query param
    path('pdf/upload', PDFValidator.as_view(), name='upload_pdf'),
    path('openapi/pdf/upload/', PDFValidator.as_view(), name='openapi_pdf_upload'),
    path('enquiry/', SubmitEnquiry.as_view(), name='enquiry_shipment'),  # for ?awb= query param
    path('enquiries/', EnquiryListView.as_view(), name='enquiry_list'),
]
