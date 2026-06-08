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

CURATED_ANTHROPIC_MODELS = [
    {
        "id": "claude-opus-4-8",
        "name": "Claude Opus 4.8",
        "sort_order": 10,
    },
    {
        "id": "claude-sonnet-4-6",
        "name": "Claude Sonnet 4.6",
        "sort_order": 20,
    },
    {
        "id": "claude-haiku-4-5",
        "name": "Claude Haiku 4.5",
        "sort_order": 30,
    },
]

CURATED_GEMINI_MODELS = [
    {
        "id": "gemini-3.5-flash",
        "name": "Gemini 3.5 Flash",
        "sort_order": 10,
    },
    {
        "id": "gemini-3.1-pro-preview",
        "name": "Gemini 3.1 Pro Preview",
        "sort_order": 20,
    },
    {
        "id": "gemini-3-flash-preview",
        "name": "Gemini 3 Flash Preview",
        "sort_order": 30,
    },
    {
        "id": "gemini-3.1-flash-lite",
        "name": "Gemini 3.1 Flash-Lite",
        "sort_order": 40,
    },
    {
        "id": "gemini-2.5-pro",
        "name": "Gemini 2.5 Pro",
        "sort_order": 50,
    },
    {
        "id": "gemini-2.5-flash",
        "name": "Gemini 2.5 Flash",
        "sort_order": 60,
    },
    {
        "id": "gemini-2.5-flash-lite",
        "name": "Gemini 2.5 Flash-Lite",
        "sort_order": 70,
    },
]

CURATED_MODELS_BY_PROVIDER = {
    AIProviderKey.Provider.OPENAI: CURATED_OPENAI_MODELS,
    AIProviderKey.Provider.ANTHROPIC: CURATED_ANTHROPIC_MODELS,
    AIProviderKey.Provider.GEMINI: CURATED_GEMINI_MODELS,
}

DEFAULT_MODEL_BY_PROVIDER = {
    AIProviderKey.Provider.OPENAI: "gpt-5.4-mini",
    AIProviderKey.Provider.ANTHROPIC: "claude-sonnet-4-6",
    AIProviderKey.Provider.GEMINI: "gemini-3.5-flash",
}


def seed_curated_model_catalog():
    for provider, models in CURATED_MODELS_BY_PROVIDER.items():
        for model in models:
            AIModelCatalog.objects.update_or_create(
                provider=provider,
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

    provider_models = CURATED_MODELS_BY_PROVIDER.get(provider, [])
    if provider_models:
        return [(model["id"], model["name"]) for model in provider_models]

    return []


def get_curated_model_choices_by_provider():
    return {
        provider: [
            {"id": model_id, "name": display_name}
            for model_id, display_name in get_curated_model_choices(provider)
        ]
        for provider in CURATED_MODELS_BY_PROVIDER
    }


def get_default_model_for_provider(provider):
    default_model = DEFAULT_MODEL_BY_PROVIDER.get(provider)
    choices = get_curated_model_choices(provider)
    choice_values = {model_id for model_id, _display_name in choices}
    if default_model in choice_values:
        return default_model
    if choices:
        return choices[0][0]
    return ""


def get_default_models_by_provider():
    return {
        provider: get_default_model_for_provider(provider)
        for provider in CURATED_MODELS_BY_PROVIDER
    }


def sync_provider_models(provider_key, allowed_model_ids=None):
    if provider_key.provider not in CURATED_MODELS_BY_PROVIDER:
        raise ModelSyncError("This provider does not have a curated model catalog.")

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
