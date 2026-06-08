from django.db import OperationalError, ProgrammingError
from django.utils import timezone

from .models import AIModelCatalog, AIProviderKey, AIProviderModel


class ModelSyncError(Exception):
    pass


CURATED_OPENAI_MODELS = [
    {
        "id": "gpt-5.5",
        "name": "GPT-5.5",
        "sort_order": 10,
    },
    {
        "id": "gpt-5.4",
        "name": "GPT-5.4",
        "sort_order": 20,
    },
    {
        "id": "gpt-5.4-mini",
        "name": "GPT-5.4 mini",
        "sort_order": 30,
    },
    {
        "id": "gpt-5.4-nano",
        "name": "GPT-5.4 nano",
        "sort_order": 40,
    },
    {
        "id": "gpt-4.1",
        "name": "GPT-4.1",
        "sort_order": 50,
    },
    {
        "id": "gpt-4.1-mini",
        "name": "GPT-4.1 mini",
        "sort_order": 60,
    },
    {
        "id": "gpt-4.1-nano",
        "name": "GPT-4.1 nano",
        "sort_order": 70,
    },
]


def seed_curated_model_catalog():
    for model in CURATED_OPENAI_MODELS:
        AIModelCatalog.objects.update_or_create(
            provider=AIProviderKey.Provider.OPENAI,
            model_id=model["id"],
            defaults={
                "display_name": model["name"],
                "enabled": True,
                "sort_order": model["sort_order"],
            },
        )


def get_curated_model_choices(provider):
    catalog_models = AIModelCatalog.objects.filter(
        provider=provider,
        enabled=True,
    ).order_by("sort_order", "display_name")

    try:
        choices = [(model.model_id, model.display_name) for model in catalog_models]
    except (OperationalError, ProgrammingError):
        choices = []
    if choices:
        return choices

    if provider == AIProviderKey.Provider.OPENAI:
        return [(model["id"], model["name"]) for model in CURATED_OPENAI_MODELS]

    return []


def sync_provider_models(provider_key, allowed_model_ids=None):
    if provider_key.provider != AIProviderKey.Provider.OPENAI:
        raise ModelSyncError("Only OpenAI curated model refresh is supported right now.")

    now = timezone.now()
    seen_model_ids = set()
    if allowed_model_ids is not None:
        allowed_model_ids = set(allowed_model_ids)
    catalog_models = AIModelCatalog.objects.filter(
        provider=provider_key.provider,
        enabled=True,
    ).order_by("sort_order", "display_name")

    for model in catalog_models:
        model_id = model.model_id
        seen_model_ids.add(model_id)
        defaults = {
            "display_name": model.display_name,
            "allowed": True if allowed_model_ids is None else model_id in allowed_model_ids,
            "available": True,
            "last_seen_at": now,
        }
        provider_model, created = AIProviderModel.objects.get_or_create(
            provider_key=provider_key,
            provider_model_id=model_id,
            defaults=defaults,
        )
        if not created:
            provider_model.display_name = model.display_name
            provider_model.available = True
            provider_model.last_seen_at = now
            if allowed_model_ids is not None:
                provider_model.allowed = model_id in allowed_model_ids
            provider_model.save(
                update_fields=[
                    "display_name",
                    "allowed",
                    "available",
                    "last_seen_at",
                    "updated_at",
                ]
            )

    provider_key.available_models.exclude(
        provider_model_id__in=seen_model_ids
    ).update(available=False)

    return len(seen_model_ids)
