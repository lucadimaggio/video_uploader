"""
api_instagram.py — Upload Reels su Instagram via Meta Graph API
ENV: IG_ACCOUNT_ID, META_USER_TOKEN
"""
import os
import time
import logging
import requests

logger = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com/v19.0"


def upload_reel(video_url: str, caption: str) -> dict:
    user_id = os.environ["IG_ACCOUNT_ID"]
    token   = os.environ["META_USER_TOKEN"]

    logger.info(f"[IG] Creo container per: {video_url}")
    r1 = requests.post(f"{GRAPH_URL}/{user_id}/media", data={
        "media_type":    "REELS",
        "video_url":     video_url,
        "caption":       caption,
        "share_to_feed": True,
        "access_token":  token,
    })
    _log_raw("container_create", r1)

    body1 = r1.json()
    if r1.status_code != 200 or "id" not in body1:
        return _error_result("Container creation failed", body1)

    container_id = body1["id"]
    logger.info(f"[IG] Container ID: {container_id} — polling...")

    status = _poll_status(container_id, token)
    if status != "FINISHED":
        return {
            "success": False, "post_id": None,
            "error": f"Container status: {status} (atteso FINISHED)",
            "details": {"container_id": container_id, "status": status}
        }

    r3 = requests.post(f"{GRAPH_URL}/{user_id}/media_publish", data={
        "creation_id":  container_id,
        "access_token": token,
    })
    _log_raw("media_publish", r3)

    body3 = r3.json()
    if r3.status_code == 200 and "id" in body3:
        logger.info(f"[IG] Pubblicato — post_id: {body3['id']}")
        return {"success": True, "post_id": body3["id"], "error": None, "details": body3}

    return _error_result("Publish failed", body3)


def _poll_status(container_id: str, token: str, max_attempts: int = 12, interval: int = 10) -> str:
    for i in range(1, max_attempts + 1):
        r = requests.get(f"{GRAPH_URL}/{container_id}", params={
            "fields": "status_code,status",
            "access_token": token
        })
        _log_raw(f"poll_{i}", r)
        status_code = r.json().get("status_code", "UNKNOWN")
        logger.info(f"[IG] Poll {i}/{max_attempts}: {status_code}")
        if status_code in ("FINISHED", "ERROR", "EXPIRED"):
            return status_code
        time.sleep(interval)
    return "TIMEOUT"


def _error_result(prefix: str, body: dict) -> dict:
    err = body.get("error", {})
    return {
        "success": False, "post_id": None,
        "error": f"{prefix}: {err.get('message', 'unknown')} (subcode: {err.get('error_subcode', 'N/A')})",
        "details": body
    }


def _log_raw(label: str, response: requests.Response):
    try:
        body = response.json()
    except Exception:
        body = response.text
    logger.info(f"[IG RAW] {label} | status={response.status_code} | {body}")
