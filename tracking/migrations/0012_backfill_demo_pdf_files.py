from pathlib import Path
import shutil

from django.conf import settings
from django.db import migrations


PDF_SOURCES = {
    "shiprocket-1.pdf": Path("test_labels/shiprocket/real/shiprocket-1.pdf"),
    "xpress-2.pdf": Path("test_labels/xpressbees/real/xpress-2.pdf"),
    "delhivery-1.pdf": Path("test_labels/delhivery/real/delhivery-1.pdf"),
}


def backfill_demo_pdf_files(apps, schema_editor):
    PdfValidationRecord = apps.get_model("tracking", "PdfValidationRecord")

    for record in PdfValidationRecord.objects.filter(file_name__in=PDF_SOURCES):
        details = dict(record.details or {})
        if details.get("stored_pdf"):
            continue

        source = Path(settings.BASE_DIR) / PDF_SOURCES[record.file_name]
        if not source.is_file():
            continue

        relative_path = Path("shipment_labels") / f"merchant_{record.merchant_id}" / f"{record.id}_{record.file_name}"
        destination = Path(settings.MEDIA_ROOT) / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

        details["stored_pdf"] = relative_path.as_posix()
        record.details = details
        record.save(update_fields=["details"])


class Migration(migrations.Migration):
    dependencies = [("tracking", "0011_whatsappmessagelog")]

    operations = [migrations.RunPython(backfill_demo_pdf_files, migrations.RunPython.noop)]
