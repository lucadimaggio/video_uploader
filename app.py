"""
app.py — FastAPI server (Railway)
Endpoint principale: POST /upload
Chiamato da n8n con video_url (Google Drive direct download), filename, caption.
"""
import os
import json
import logging
import tempfile
import requests as req_lib

from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel

from video_utils import sanitize_filename, check_instagram_requirements
from api_instagram import upload_reel
from api_youtube import upload_video as yt_upload
from api_facebook import upload_video as fb_upload
from api_r2 import upload_to_r2, delete_from_r2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Video Uploader")

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "")


# ── Auth ─────────────────────────────────────────────────────────────────────

def verify_api_key(x_api_key: str = Header(...)):
    if INTERNAL_API_KEY and x_api_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Models ────────────────────────────────────────────────────────────────────

class UploadRequest(BaseModel):
    video_url:  str           # Google Drive direct download URL
    filename:   str           # Nome originale (verrà sanitizzato)
    caption:    str
    platforms:  list[str] = ["youtube", "facebook", "instagram"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def notify_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("[TELEGRAM] Credenziali mancanti")
        return
    try:
        req_lib.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        logger.warning(f"[TELEGRAM] Invio fallito: {e}")


def download_video(url: str, dest_path: str):
    logger.info(f"[DOWNLOAD] {url} → {dest_path}")
    r = req_lib.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/upload", dependencies=[Depends(verify_api_key)])
def upload(body: UploadRequest):
    """
    Pipeline completa:
    1. Scarica video da Google Drive
    2. Sanitizza filename
    3. Upload su R2 (genera URL pubblico per IG/FB)
    4. Check requisiti IG
    5. Pubblica su YT, FB, IG
    6. Cleanup R2 e file locale
    """
    safe_name = sanitize_filename(body.filename)
    results   = {}
    r2_key    = None

    # ── 1. Download locale ────────────────────────────────────────────────────
    tmp_dir  = tempfile.mkdtemp()
    filepath = os.path.join(tmp_dir, safe_name)
    try:
        download_video(body.video_url, filepath)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download fallito: {e}")

    logger.info(f"[PIPELINE] File pronto: {filepath} ({os.path.getsize(filepath)/1024/1024:.1f}MB)")

    # ── 2. Upload su R2 (URL pubblico per IG e FB) ────────────────────────────
    try:
        r2_key    = f"videos/{safe_name}"
        video_url = upload_to_r2(filepath, r2_key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload R2 fallito: {e}")

    # ── 3. YouTube (da file locale) ───────────────────────────────────────────
    if "youtube" in body.platforms:
        res = yt_upload(filepath, title=body.caption)
        results["youtube"] = res
        logger.info(f"[YT] {res}")

    # ── 4. Facebook (da URL R2) ───────────────────────────────────────────────
    if "facebook" in body.platforms:
        res = fb_upload(video_url, description=body.caption)
        results["facebook"] = res
        logger.info(f"[FB] {res}")

    # ── 5. Instagram (da URL R2, con check) ───────────────────────────────────
    if "instagram" in body.platforms:
        check = check_instagram_requirements(filepath)
        logger.info(f"[IG CHECK] {check}")

        if not check["ok"]:
            err_str = "; ".join(check["errors"])
            results["instagram"] = {"success": False, "error": err_str, "check": check}
            notify_telegram(
                f"*IG Upload BLOCCATO*\n"
                f"File: `{safe_name}`\n"
                f"Errori: {err_str}"
            )
            # Non elimino da R2 per recovery manuale
            logger.warning(f"[IG] Bloccato — file R2 conservato: {r2_key}")
        else:
            for w in check.get("warnings", []):
                logger.warning(f"[IG CHECK] WARNING: {w}")

            res = upload_reel(video_url, body.caption)
            results["instagram"] = res
            logger.info(f"[IG] {res}")

            if not res["success"]:
                notify_telegram(
                    f"*IG Upload FALLITO*\n"
                    f"File: `{safe_name}`\n"
                    f"Errore: {res.get('error')}\n"
                    f"Dettagli: `{json.dumps(res.get('details', {}), ensure_ascii=False)[:300]}`"
                )
                logger.warning(f"[IG] Fallito — file R2 conservato per recovery: {r2_key}")
                r2_key = None  # Non eliminare

    # ── 6. Cleanup ────────────────────────────────────────────────────────────
    try:
        os.remove(filepath)
        os.rmdir(tmp_dir)
    except Exception:
        pass

    if r2_key:
        try:
            delete_from_r2(r2_key)
        except Exception as e:
            logger.warning(f"[R2] Cleanup fallito: {e}")

    return {"filename": safe_name, "results": results}
