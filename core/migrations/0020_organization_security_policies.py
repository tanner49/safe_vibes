from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0019_reportdatasetcachelock"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="report_ip_allowlist_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="organization",
            name="report_ip_allowlist",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="organization",
            name="report_url_whitelist_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="organization",
            name="report_url_whitelist",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="organization",
            name="report_url_blacklist_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="organization",
            name="report_url_blacklist",
            field=models.TextField(blank=True),
        ),
    ]
