from django.db import migrations


CURATED_OPENAI_MODELS = [
    ("gpt-5.5", "GPT-5.5", 10),
    ("gpt-5.4", "GPT-5.4", 20),
    ("gpt-5.4-mini", "GPT-5.4 mini", 30),
    ("gpt-5.4-nano", "GPT-5.4 nano", 40),
    ("gpt-4.1", "GPT-4.1", 50),
    ("gpt-4.1-mini", "GPT-4.1 mini", 60),
    ("gpt-4.1-nano", "GPT-4.1 nano", 70),
]


def seed_openai_model_catalog(apps, schema_editor):
    AIModelCatalog = apps.get_model("core", "AIModelCatalog")
    for model_id, display_name, sort_order in CURATED_OPENAI_MODELS:
        AIModelCatalog.objects.update_or_create(
            provider="openai",
            model_id=model_id,
            defaults={
                "display_name": display_name,
                "enabled": True,
                "sort_order": sort_order,
            },
        )


def unseed_openai_model_catalog(apps, schema_editor):
    AIModelCatalog = apps.get_model("core", "AIModelCatalog")
    AIModelCatalog.objects.filter(
        provider="openai",
        model_id__in=[model_id for model_id, _display_name, _sort_order in CURATED_OPENAI_MODELS],
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0006_aimodelcatalog"),
    ]

    operations = [
        migrations.RunPython(seed_openai_model_catalog, unseed_openai_model_catalog),
    ]
