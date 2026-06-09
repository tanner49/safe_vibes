from django.db.models.signals import post_save
from django.dispatch import receiver

from .demo_database import ensure_demo_database_connection
from .models import Organization


@receiver(post_save, sender=Organization)
def create_demo_database_connection(sender, instance, created, **kwargs):
    if created:
        ensure_demo_database_connection(instance)
