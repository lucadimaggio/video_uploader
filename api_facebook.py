"""
api_facebook.py — Upload video su Facebook Page via Meta Graph API
ENV: FB_PAGE_ID, META_PAGE_TOKEN
"""
import os
import logging
import requests

logger = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com/v19.0"


def upload_video(video_url: str, description: str = "", thumb_url: str = "") -> dict:
    page_id = os.environ["FB_PAGE_ID"]
    token   = os.environ["META_PAGE_TOKEN"]

    logger.info(f"[FB] Upload video da URL: {video_url}")
    payload = {
        "file_url":     video_url,
        "description":  description,
        "published":    True,
        "access_token": token,
    }
    if thumb_url:
        payload["thumb_url"] = thumb_url
    r = requests.post(f"{GRAPH_URL}/{page_id}/videos", data=payload)
    logger.info(f"[FB RAW] status={r.status_code} | {r.text[:300]}")

    body = r.json()
    if r.status_code == 200 and "id" in body:
        logger.info(f"[FB] Pubblicato — post_id: {body['id']}")
        return {"success": True, "post_id": body["id"], "error": None}

    return {"success": False, "post_id": None, "error": body.get("error", {}).get("message", "unknown")}
