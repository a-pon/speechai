"""Private Object Storage objects for SpeechKit. Never set public ACLs."""
from pathlib import Path

from app.config import get_settings


def _client():
    import boto3
    from botocore.config import Config

    settings = get_settings()
    if not all((settings.object_storage_bucket, settings.object_storage_access_key_id,
                settings.object_storage_secret_access_key)):
        raise RuntimeError("Object Storage не настроен: задайте bucket и статические ключи сервисного аккаунта")
    return boto3.client(
        "s3", endpoint_url=settings.object_storage_endpoint,
        aws_access_key_id=settings.object_storage_access_key_id,
        aws_secret_access_key=settings.object_storage_secret_access_key,
        region_name="ru-central1",
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def upload_audio(path: Path, key: str) -> str:
    settings = get_settings()
    from boto3.s3.transfer import TransferConfig

    client = _client()
    # Keep multipart upload bounded on the small worker container.
    transfer = TransferConfig(multipart_threshold=8 * 1024 * 1024,
                              multipart_chunksize=8 * 1024 * 1024,
                              max_concurrency=1, use_threads=False)
    client.upload_file(str(path), settings.object_storage_bucket, key, Config=transfer)
    metadata = client.head_object(Bucket=settings.object_storage_bucket, Key=key)
    if metadata["ContentLength"] != path.stat().st_size:
        raise RuntimeError("Object Storage: размер загруженного объекта отличается от локального файла")
    return f"{settings.object_storage_endpoint.rstrip('/')}/{settings.object_storage_bucket}/{key}"


def audio_uri(key: str) -> str:
    settings = get_settings()
    return f"{settings.object_storage_endpoint.rstrip('/')}/{settings.object_storage_bucket}/{key}"


def delete_audio(key: str) -> None:
    _client().delete_object(Bucket=get_settings().object_storage_bucket, Key=key)
