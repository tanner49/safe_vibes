from django.core.management.base import BaseCommand

from core.demo_database import ensure_demo_database_connection
from core.models import Organization


class Command(BaseCommand):
    help = "Create or refresh the bundled demo SQLite database connection for organizations."

    def handle(self, *args, **options):
        created_count = 0
        refreshed_count = 0
        skipped_count = 0
        for organization in Organization.objects.order_by("name"):
            database_connection, created = ensure_demo_database_connection(organization)
            if database_connection is None:
                skipped_count += 1
            elif created:
                created_count += 1
            else:
                refreshed_count += 1

        if options["verbosity"] > 0:
            self.stdout.write(
                self.style.SUCCESS(
                    "Demo database connections: "
                    f"{created_count} created, {refreshed_count} refreshed, {skipped_count} skipped."
                )
            )
