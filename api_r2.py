"""
api_r2.py — Upload file su Cloudflare R2 e generazione URL pubblico
ENV: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_PUBLIC_URL
"""
import os
import logging
import boto3
from botocore.client import Config

logger = logging.getLogger(__name__)


def get_r2_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def upload_to_r2(filepath: str, object_key: str) -> str:
    """
    Carica il file su R2 e ritorna l'URL pubblico.
    """
    bucket = os.environ["R2_BUCKET_NAME"]
    public_base = os.environ["R2_PUBLIC_URL"].rstrip("/")

    client = get_r2_client()
    logger.info(f"[R2] Upload {filepath} → {bucket}/{object_key}")
    client.upload_file(
        filepath, bucket, object_key,
        ExtraArgs={"ContentType": "video/mp4"}
    )
    url = f"{public_base}/{object_key}"
    logger.info(f"[R2] URL pubblico: {url}")
    return url


def delete_from_r2(object_key: str):
    """Rimuove il file da R2 dopo la pubblicazione."""
    bucket = os.environ["R2_BUCKET_NAME"]
    client = get_r2_client()
    client.delete_object(Bucket=bucket, Key=object_key)
    logger.info(f"[R2] Eliminato: {object_key}")
