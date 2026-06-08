from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils.text import slugify

from .crypto import decrypt_text, encrypt_text


class Organization(models.Model):
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    sso_required = models.BooleanField(default=False)

    query_timeout_seconds = models.PositiveIntegerField(
        default=settings.REPORT_QUERY_TIMEOUT_SECONDS
    )
    cache_ttl_seconds = models.PositiveIntegerField(default=settings.REPORT_CACHE_TTL_SECONDS)
    max_rows = models.PositiveIntegerField(default=settings.REPORT_MAX_ROWS)
    max_raw_bytes = models.PositiveBigIntegerField(default=settings.REPORT_MAX_RAW_BYTES)
    max_compressed_bytes = models.PositiveBigIntegerField(
        default=settings.REPORT_MAX_COMPRESSED_BYTES
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("core:dashboard")


class Membership(models.Model):
    class Role(models.TextChoices):
        ADMIN = "admin", "Company admin"
        CREATOR = "creator", "Creator"
        VIEWER = "viewer", "Viewer"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_memberships",
    )
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.VIEWER,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["organization__name", "user__email"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"],
                name="unique_membership_per_organization_user",
            )
        ]

    def __str__(self):
        return f"{self.user} in {self.organization} ({self.get_role_display()})"

    @property
    def is_company_admin(self):
        return self.role == self.Role.ADMIN


class AIProviderKey(models.Model):
    class Provider(models.TextChoices):
        OPENAI = "openai", "OpenAI"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="ai_provider_keys",
    )
    provider = models.CharField(
        max_length=30,
        choices=Provider.choices,
        default=Provider.OPENAI,
    )
    name = models.CharField(max_length=255, default="OpenAI")
    model_name = models.CharField(max_length=255, default="gpt-5.4-mini")
    encrypted_api_key = models.TextField()
    key_last_four = models.CharField(max_length=4, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "provider", "name"],
                name="unique_ai_provider_key_name_per_organization",
            )
        ]

    def __str__(self):
        return f"{self.get_provider_display()} - {self.name}"

    def set_api_key(self, api_key):
        self.encrypted_api_key = encrypt_text(api_key)
        self.key_last_four = api_key[-4:]

    def get_api_key(self):
        return decrypt_text(self.encrypted_api_key)

    def allowed_models_count(self):
        return self.available_models.filter(allowed=True, available=True).count()

    def synced_models_count(self):
        return self.available_models.filter(available=True).count()


class AIModelCatalog(models.Model):
    provider = models.CharField(
        max_length=30,
        choices=AIProviderKey.Provider.choices,
        default=AIProviderKey.Provider.OPENAI,
    )
    model_id = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255)
    enabled = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "display_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "model_id"],
                name="unique_ai_model_catalog_provider_model",
            )
        ]

    def __str__(self):
        return f"{self.get_provider_display()} - {self.display_name}"


class AIProviderModel(models.Model):
    provider_key = models.ForeignKey(
        AIProviderKey,
        on_delete=models.CASCADE,
        related_name="available_models",
    )
    provider_model_id = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255)
    allowed = models.BooleanField(default=False)
    available = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider_model_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider_key", "provider_model_id"],
                name="unique_model_per_ai_provider_key",
            )
        ]

    def __str__(self):
        return self.provider_model_id
