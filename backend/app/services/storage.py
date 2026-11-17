"""Object storage for finished clips. Local disk by default; S3-compatible (Cloudflare R2, B2, MinIO, AWS)
when STORAGE_BACKEND=s3. Source recordings always stay on local disk because ffmpeg needs to seek them.
"""
from .. import config


def enabled() -> bool:
    return config.STORAGE_BACKEND == "s3" and bool(config.S3_BUCKET and config.S3_ACCESS_KEY and config.S3_SECRET_KEY)


def _client():
    import boto3  # optional dependency: pip install boto3
    return boto3.client("s3", endpoint_url=config.S3_ENDPOINT or None, region_name=config.S3_REGION,
                        aws_access_key_id=config.S3_ACCESS_KEY, aws_secret_access_key=config.S3_SECRET_KEY)


def upload(local_path: str, key: str) -> str | None:
    """Copy a rendered clip to the bucket. Returns the key, or None when S3 is not configured."""
    if not enabled():
        return None
    _client().upload_file(local_path, config.S3_BUCKET, key, ExtraArgs={"ContentType": "video/mp4"})
    return key


def presigned_url(key: str, filename: str, expires: int = 3600) -> str:
    return _client().generate_presigned_url(
        "get_object", ExpiresIn=expires,
        Params={"Bucket": config.S3_BUCKET, "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"'})


def status() -> dict:
    return {"backend": "s3" if enabled() else "local", "bucket": config.S3_BUCKET if enabled() else None,
            "configured": enabled() or config.STORAGE_BACKEND == "local"}
