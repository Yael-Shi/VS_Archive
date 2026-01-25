from django.db import models


class Document(models.Model):
    title = models.CharField(max_length=255)
    language = models.CharField(max_length=16, default="he")  # he/en/ar/fr
    file_s3_key = models.CharField(max_length=1024, blank=True, default="")
    file_original_name = models.CharField(max_length=512, blank=True, default="")
    mime_type = models.CharField(max_length=128, blank=True, default="")
    size_bytes = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title
