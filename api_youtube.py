"""
api_youtube.py — Upload video su YouTube via YouTube Data API v3
ENV: YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN
"""
import os
import json
import logging
import requests

logger = logging.getLogger(__name__)

TOKEN_URL     = "https://oauth2.googleapis.com/token"
UPLOAD_URL    = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"


def _get_access_token() -> str:
    r = requests.post(TOKEN_URL, data={
        "client_id":     os.environ["YOUTUBE_CLIENT_ID"],
        "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"],
        "refresh_token": os.environ["YOUTUBE_REFRESH_TOKEN"],
        "grant_type":    "refresh_token",
    })
    r.raise_for_status()
    return r.json()["access_token"]


def upload_video(filepath: str, title: str, description: str = "", privacy: str = "public") -> dict:
    try:
        token = _get_access_token()
    except Exception as e:
        return {"success": False, "video_id": None, "error": f"Token error: {e}"}

    metadata = json.dumps({
        "snippet": {
            "title":       title[:100],
            "description": description,
            "categoryId":  "22"
        },
        "status": {"privacyStatus": privacy}
    })

    logger.info(f"[YT] Upload: {filepath}")
    try:
        with open(filepath, "rb") as f:
            r = requests.post(
                UPLOAD_URL,
                params={"uploadType": "multipart", "part": "snippet,status"},
                headers={"Authorization": f"Bearer {token}"},
                files={
                    "metadata": (None, metadata, "application/json; charset=UTF-8"),
                    "video":    (os.path.basename(filepath), f, "video/*"),
                }
            )
        logger.info(f"[YT RAW] status={r.status_code} | {r.text[:300]}")
        body = r.json()
        if r.status_code in (200, 201) and "id" in body:
            logger.info(f"[YT] Upload OK — video_id: {body['id']}")
            return {"success": True, "video_id": body["id"], "error": None}
        return {"success": False, "video_id": None, "error": body.get("error", {}).get("message", "unknown")}
    except Exception as e:
        return {"success": False, "video_id": None, "error": str(e)}


def set_thumbnail(video_id: str, thumbnail_path: str) -> bool:
    try:
        token = _get_access_token()
        with open(thumbnail_path, "rb") as f:
            r = requests.post(
                THUMBNAIL_URL,
                params={"videoId": video_id},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
                data=f
            )
        logger.info(f"[YT THUMBNAIL] status={r.status_code} | {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"[YT THUMBNAIL] Fallita: {e}")
        return False
