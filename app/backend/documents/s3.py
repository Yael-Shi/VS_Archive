import boto3
from django.conf import settings


def get_s3_client():
    return boto3.client(
        "s3",
        region_name=getattr(settings, "AWS_REGION", None),
    )


def create_presigned_put(
    bucket: str,
    key: str,
    content_type: str,
    expires_in: int = 3600,
) -> str:
    s3 = get_s3_client()
    return s3.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ContentType": content_type,
        },
        ExpiresIn=expires_in,
    )


def create_presigned_get(
    bucket: str,
    key: str,
    expires_in: int = 3600,
) -> str:
    s3 = get_s3_client()
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ResponseContentDisposition": "inline",
        },
        ExpiresIn=expires_in,
    )
