from django.db import models


class PublicContentBlock(models.Model):
    """Editable public-site copy (biography, about, contact)."""

    key = models.CharField(max_length=64, unique=True)
    title = models.CharField(max_length=255, blank=True, default="")
    body = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]

    def __str__(self) -> str:
        return self.key
