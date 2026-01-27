import os
import boto3

def s3_client():
    return boto3.client("s3", region_name=os.getenv("AWS_REGION"))

def create_presigned_put(bucket: str, key: str, content_type: str, expires_in: int = 900) -> str:
    return s3_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=expires_in,
    )
