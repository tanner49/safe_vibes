from django.core.management.base import BaseCommand

from core.report_cache import cleanup_expired_report_dataset_caches


class Command(BaseCommand):
    help = "Delete expired report dataset cache rows."

    def handle(self, *args, **options):
        deleted_count, _details = cleanup_expired_report_dataset_caches()
        self.stdout.write(
            self.style.SUCCESS(f"Deleted {deleted_count} expired report dataset caches.")
        )
