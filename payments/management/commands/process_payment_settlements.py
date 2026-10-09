from django.core.management.base import BaseCommand

from payments.services.settlements import process_due_settlements


class Command(BaseCommand):
    help = "Poll configured payment providers for due settlement updates."

    def add_arguments(self, parser):
        parser.add_argument("--provider", default=None)

    def handle(self, *args, **options):
        results = process_due_settlements(options.get("provider"))
        self.stdout.write(self.style.SUCCESS(f"Checked {len(results)} settlement records."))
