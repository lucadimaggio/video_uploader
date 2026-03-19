"""
app.py — FastAPI server (Railway)
Flusso ibrido: n8n orchestra, FastAPI gestisce le parti Python.

Endpoints:
  POST /generate              — scarica video, Gemini, thumbnail, uploda R2
  POST /publish/youtube       — pubblica su YouTube + thumbnail
  POST /publish/facebook      — pubblica su Facebook + thumbnail
  POST /publish/instagram     — pubblica su Instagram + cover
  GET  /health
"""
import os
import json
import logging
import tempfile
import requests as req_lib

from fastapi import FastAPI, HTTPException, Header, Depends, UploadFile, File, Form
from pydantic import BaseModel, field_validator

from video_utils import sanitize_filename, check_instagram_requirements
from api_instagram import upload_reel
from api_youtube import upload_video as yt_upload, set_thumbnail as yt_set_thumbnail
from api_facebook import upload_video as fb_upload
from api_r2 import upload_to_r2, delete_from_r2
from api_gemini import generate_metadata
from api_thumbnail import generate_thumbnail

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Video Uploader")

INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "")


# ── Auth ──────────────────────────────────────────────────────────────────────

def verify_api_key(x_api_key: str = Header(...)):
    if INTERNAL_API_KEY and x_api_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Models ────────────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    video_url: str = ""
    filename:  str


class PublishYouTubeRequest(BaseModel):
    r2_video_url:  str
    r2_video_key:  str
    r2_thumb_url:  str = ""
    r2_thumb_key:  str = ""
    safe_filename: str
    yt_title:      str = ""
    yt_description: str = ""


class PublishFacebookRequest(BaseModel):
    r2_video_url:  str
    r2_thumb_url:  str = ""
    fb_description: str = ""


class PublishInstagramRequest(BaseModel):
    r2_video_url:  str
    r2_thumb_url:  str = ""
    ig_caption:    str = ""
    safe_filename: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def download_video(url: str, dest_path: str):
    logger.info(f"[DOWNLOAD] {url}")
    session = req_lib.Session()
    r = session.get(url, stream=True, timeout=180)
    r.raise_for_status()

    # Google Drive mostra pagina di conferma per file grandi — gestisci il token
    confirm_token = None
    for key, value in r.cookies.items():
        if key.startswith("download_warning"):
            confirm_token = value
            break
    if confirm_token:
        logger.info("[DOWNLOAD] Google Drive conferma richiesta — retry con token")
        r = session.get(url, params={"confirm": confirm_token}, stream=True, timeout=180)
        r.raise_for_status()

    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=32768):
            if chunk:
                f.write(chunk)
    logger.info(f"[DOWNLOAD] Completato: {os.path.getsize(dest_path)/1024/1024:.1f}MB")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/generate", dependencies=[Depends(verify_api_key)])
async def generate(
    file: UploadFile = File(None),
    filename: str = Form(None),
    video_url: str = Form(None),
    x_api_key: str = Header(None)
):
    """
    Accetta file binario (multipart) da n8n oppure URL.
    1. Salva/scarica il video
    2. Genera metadati con Gemini
    3. Genera thumbnail con ffmpeg + Pillow
    4. Uploda video e thumbnail su R2
    5. Restituisce URLs R2 + metadati per n8n
    """
    if INTERNAL_API_KEY and x_api_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    import uuid
    job_id    = str(uuid.uuid4())[:8]
    safe_name = sanitize_filename(filename or (file.filename if file else "video.mp4"))

    tmp_dir  = tempfile.mkdtemp()
    filepath = os.path.join(tmp_dir, safe_name)

    if file:
        # Ricezione file binario da n8n
        logger.info(f"[GENERATE] Ricezione file binario: {safe_name}")
        try:
            content = await file.read()
            with open(filepath, "wb") as f:
                f.write(content)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Ricezione file fallita: {e}")
    elif video_url:
        # Fallback: download da URL
        try:
            download_video(video_url, filepath)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Download fallito: {e}")
    else:
        raise HTTPException(status_code=400, detail="Richiesto 'file' o 'video_url'")

    logger.info(f"[GENERATE] File: {filepath} ({os.path.getsize(filepath)/1024/1024:.1f}MB)")

    # Gemini metadati
    meta = generate_metadata(filepath)
    logger.info(f"[GENERATE] Metadati: {meta}")

    # Thumbnail
    thumb_r2_url = ""
    thumb_r2_key = ""
    thumb_text   = meta.get("thumbnail_text") or meta.get("yt_title") or safe_name.replace("_", " ").replace(".mp4", "")
    thumb_path   = generate_thumbnail(filepath, thumb_text) if thumb_text else None
    if thumb_path:
        try:
            thumb_r2_key = f"thumbnails/{job_id}/thumb.jpg"
            thumb_r2_url = upload_to_r2(thumb_path, thumb_r2_key)
            logger.info(f"[GENERATE] Thumbnail R2: {thumb_r2_url}")
        except Exception as e:
            logger.warning(f"[GENERATE] Thumbnail R2 upload fallito: {e}")
        finally:
            try:
                os.remove(thumb_path)
            except Exception:
                pass

    # Upload video su R2
    try:
        r2_key = f"videos/{job_id}/{safe_name}"
        r2_url = upload_to_r2(filepath, r2_key)
        logger.info(f"[GENERATE] Video R2: {r2_url}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload R2 fallito: {e}")
    finally:
        try:
            os.remove(filepath)
            os.rmdir(tmp_dir)
        except Exception:
            pass

    return {
        "job_id":        job_id,
        "safe_filename": safe_name,
        "r2_video_url":  r2_url,
        "r2_video_key":  r2_key,
        "r2_thumb_url":  thumb_r2_url,
        "r2_thumb_key":  thumb_r2_key,
        **meta
    }


@app.post("/publish/youtube", dependencies=[Depends(verify_api_key)])
def publish_youtube(body: PublishYouTubeRequest):
    """Scarica da R2, pubblica su YouTube, imposta thumbnail, cleanup R2."""
    tmp_dir  = tempfile.mkdtemp()
    filepath = os.path.join(tmp_dir, body.safe_filename)

    try:
        download_video(body.r2_video_url, filepath)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download R2 fallito: {e}")

    title = body.yt_title or body.safe_filename.replace("_", " ").replace(".mp4", "")
    res   = yt_upload(filepath, title=title, description=body.yt_description, privacy="public")
    logger.info(f"[YT] {res}")

    # Thumbnail YouTube
    if res.get("success") and res.get("video_id") and body.r2_thumb_url:
        tmp_thumb = os.path.join(tmp_dir, "thumb.jpg")
        try:
            r = req_lib.get(body.r2_thumb_url, timeout=30)
            with open(tmp_thumb, "wb") as f:
                f.write(r.content)
            yt_set_thumbnail(res["video_id"], tmp_thumb)
        except Exception as e:
            logger.warning(f"[YT THUMBNAIL] {e}")
        finally:
            try:
                os.remove(tmp_thumb)
            except Exception:
                pass

    # Cleanup locale (non R2 — viene cancellato da /cleanup alla fine)
    try:
        os.remove(filepath)
        os.rmdir(tmp_dir)
    except Exception:
        pass

    return res


@app.post("/publish/facebook", dependencies=[Depends(verify_api_key)])
def publish_facebook(body: PublishFacebookRequest):
    """Pubblica su Facebook con URL R2."""
    res = fb_upload(
        body.r2_video_url,
        description=body.fb_description,
        thumb_url=body.r2_thumb_url
    )
    logger.info(f"[FB] {res}")
    return res


@app.post("/publish/instagram", dependencies=[Depends(verify_api_key)])
def publish_instagram(body: PublishInstagramRequest):
    """Controlla requisiti IG, pubblica Reel con cover."""
    tmp_dir  = tempfile.mkdtemp()
    filepath = os.path.join(tmp_dir, body.safe_filename)

    try:
        download_video(body.r2_video_url, filepath)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download R2 fallito: {e}")

    check = check_instagram_requirements(filepath)
    logger.info(f"[IG CHECK] {check}")

    try:
        os.remove(filepath)
        os.rmdir(tmp_dir)
    except Exception:
        pass

    if not check["ok"]:
        err_str = "; ".join(check["errors"])
        return {"success": False, "error": f"Requisiti IG non soddisfatti: {err_str}"}

    caption = body.ig_caption or body.safe_filename.replace("_", " ").replace(".mp4", "")
    res     = upload_reel(body.r2_video_url, caption, cover_url=body.r2_thumb_url)
    logger.info(f"[IG] {res}")
    return res


class CleanupRequest(BaseModel):
    r2_video_key: str = ""
    r2_thumb_key: str = ""

    @field_validator("r2_video_key", "r2_thumb_key", mode="before")
    @classmethod
    def none_to_empty(cls, v):
        return v or ""


@app.post("/cleanup", dependencies=[Depends(verify_api_key)])
def cleanup(body: CleanupRequest):
    """Elimina video e thumbnail da R2 dopo la pubblicazione su tutte le piattaforme."""
    deleted = []
    for key in [body.r2_video_key, body.r2_thumb_key]:
        if key:
            try:
                delete_from_r2(key)
                deleted.append(key)
                logger.info(f"[CLEANUP] Eliminato R2: {key}")
            except Exception as e:
                logger.warning(f"[CLEANUP] Fallito per {key}: {e}")
    return {"deleted": deleted}
