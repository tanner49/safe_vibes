from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0020_organization_security_policies"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="sso_oidc_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="organization",
            name="sso_oidc_issuer_url",
            field=models.URLField(blank=True),
        ),
        migrations.AddField(
            model_name="organization",
            name="sso_oidc_client_id",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="organization",
            name="encrypted_sso_oidc_client_secret",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="organization",
            name="sso_oidc_client_secret_last_four",
            field=models.CharField(blank=True, max_length=4),
        ),
        migrations.AddField(
            model_name="organization",
            name="sso_oidc_scopes",
            field=models.CharField(default="openid email profile", max_length=255),
        ),
    ]
