from django.conf import settings
from django.db import models


class CloudIntegration(models.Model):
    PROVIDER_GOOGLE_DRIVE = "google_drive"
    PROVIDER_DROPBOX = "dropbox"
    PROVIDER_ONEDRIVE = "onedrive"
    PROVIDER_BOX = "box"

    PROVIDER_CHOICES = (
        (PROVIDER_GOOGLE_DRIVE, "Google Drive"),
        (PROVIDER_DROPBOX, "Dropbox"),
        (PROVIDER_ONEDRIVE, "OneDrive"),
        (PROVIDER_BOX, "Box"),
    )

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="cloud_integrations")
    provider = models.CharField(max_length=30, choices=PROVIDER_CHOICES)
    account_email = models.EmailField(max_length=254, blank=True)
    access_token = models.TextField(blank=True)
    refresh_token = models.TextField(blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ia_cloud_integrations"
        verbose_name = "Cloud Integration"
        verbose_name_plural = "Cloud Integrations"
        constraints = [
            models.UniqueConstraint(fields=["user", "provider"], name="uniq_cloud_integration_user_provider"),
        ]

    def __str__(self):
        return f"{self.user_id} - {self.provider}"
