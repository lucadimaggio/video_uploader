"""
app.py — FastAPI server (Railway)
Flusso ibrido: n8n orchestra, FastAPI gestisce le parti Python.

Endpoints:
  POST /generate              — scarica video, Gemini, thumbnail, uploda R2
  POST /publish/youtube       — pubblica su YouTube + thumbnail
  POST /publish/facebook      — pubblica su Facebook + thumbnail
  POST /publish/instagram     — pubblica su Instagram + cover
  POST /compress-for-ig       — reencoda video in h264/aac < max_size_mb, sovrascrive R2
  GET  /health
"""
import os
import json
import logging
import tempfile
import subprocess
import uuid
import requests as req_lib

from fastapi import FastAPI, HTTPException, Header, Depends, UploadFile, File, Form
from pydantic import BaseModel, field_validator

from video_utils import sanitize_filename, check_instagram_requirements, get_video_info
from api_instagram import upload_reel
from api_youtube import upload_video as yt_upload, set_thumbnail as yt_set_thumbnail
from api_facebook import upload_video as fb_upload
from api_r2 import upload_to_r2, delete_from_r2
from api_gemini import (
    GeminiGenerationError,
    IncompleteGeminiMetadataError,
    generate_metadata,
    validate_gemini_config,
)
from api_thumbnail import generate_thumbnail

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Video Uploader")

INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "")


@app.on_event("startup")
def startup_checks():
    validate_gemini_config(ping=os.environ.get("GEMINI_STARTUP_PING", "false").lower() == "true")


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
    r2_video_url:   str
    r2_thumb_url:   str = ""
    ig_caption:     str = ""
    safe_filename:  str
    ig_was_retried: bool = False


class CompressForIGRequest(BaseModel):
    r2_video_url: str
    r2_video_key: str
    max_size_mb:  float = 95.0


class StepMetadataRequest(BaseModel):
    r2_video_url:  str
    r2_video_key:  str
    safe_filename: str
    job_id:        str = ""


class StepThumbnailRequest(BaseModel):
    r2_video_url:  str
    r2_video_key:  str
    r2_thumb_key:  str = ""
    safe_filename: str
    job_id:        str = ""
    thumbnail_text: str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def download_video(url: str, dest_path: str):
    logger.info(f"[DOWNLOAD] {url}")
    session = req_lib.Session()
    r = session.get(url, stream=True, timeout=180)
    r.raise_for_status()
    # Google Drive mostra pagina di conferma per file grandi ? gestisci il token
    confirm_token = None
    for key, value in r.cookies.items():
        if key.startswith("download_warning"):
            confirm_token = value
            break
    if confirm_token:
        logger.info("[DOWNLOAD] Google Drive conferma richiesta ? retry con token")
        r = session.get(url, params={"confirm": confirm_token}, stream=True, timeout=180)
        r.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=32768):
            if chunk:
                f.write(chunk)
    logger.info(f"[DOWNLOAD] Completato: {os.path.getsize(dest_path)/1024/1024:.1f}MB")


def send_telegram_alert(message: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("[TELEGRAM] Env TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID mancante, alert non inviato")
        return

    try:
        response = req_lib.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=5,
        )
        response.raise_for_status()
        logger.info("[TELEGRAM] Alert inviato")
    except Exception as e:
        logger.warning(f"[TELEGRAM] Alert fallito: {e}")



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
    source_filename = filename or (file.filename if file else "video.mp4")
    safe_name = sanitize_filename(source_filename)

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
    try:
        meta = generate_metadata(filepath, filename=source_filename)
    except GeminiGenerationError as e:
        raise HTTPException(status_code=e.http_status, detail=e.to_n8n_detail())
    except IncompleteGeminiMetadataError as e:
        raise HTTPException(status_code=422, detail={"error": "parse_error", "detail": str(e)})
    except Exception as e:
        logger.exception("[GENERATE] Errore Gemini non classificato")
        raise HTTPException(status_code=502, detail={"error": "gemini_error", "detail": str(e)})
    logger.info(f"[GENERATE] Metadati: {meta}")
    if not meta.get("yt_title", "").strip() or not meta.get("thumbnail_text", "").strip():
        raise HTTPException(
            status_code=422,
            detail={"error": "parse_error", "detail": "Gemini ha restituito metadati incompleti (titolo o thumbnail_text vuoto)"},
        )

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


@app.post("/step/upload", dependencies=[Depends(verify_api_key)])
async def step_upload(
    file: UploadFile = File(None),
    filename: str = Form(None),
    video_url: str = Form(None),
    x_api_key: str = Header(None)
):
    """Passo 1/3: riceve o scarica il video, lo carica su R2. Restituisce job_id + URL R2."""
    if INTERNAL_API_KEY and x_api_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    job_id      = str(uuid.uuid4())[:8]
    source_name = filename or (file.filename if file else "video.mp4")
    safe_name   = sanitize_filename(source_name)

    tmp_dir  = tempfile.mkdtemp()
    filepath = os.path.join(tmp_dir, safe_name)

    try:
        if file:
            logger.info(f"[STEP/UPLOAD] Ricezione file binario: {safe_name}")
            try:
                content = await file.read()
                with open(filepath, "wb") as f:
                    f.write(content)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Ricezione file fallita: {e}")
        elif video_url:
            logger.info(f"[STEP/UPLOAD] Download da URL: {video_url}")
            try:
                download_video(video_url, filepath)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Download fallito: {e}")
        else:
            raise HTTPException(status_code=400, detail="Richiesto 'file' o 'video_url'")

        size_mb = os.path.getsize(filepath) / 1024 / 1024
        logger.info(f"[STEP/UPLOAD] File salvato: {filepath} ({size_mb:.1f}MB)")

        try:
            r2_key = f"videos/{job_id}/{safe_name}"
            r2_url = upload_to_r2(filepath, r2_key)
            logger.info(f"[STEP/UPLOAD] Video R2: {r2_url}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Upload R2 fallito: {e}")

        return {
            "job_id":        job_id,
            "safe_filename": safe_name,
            "r2_video_url":  r2_url,
            "r2_video_key":  r2_key,
            "size_mb":       round(size_mb, 1),
        }
    finally:
        try:
            os.remove(filepath)
            os.rmdir(tmp_dir)
        except Exception:
            pass


@app.post("/step/metadata", dependencies=[Depends(verify_api_key)])
def step_metadata(body: StepMetadataRequest):
    """Passo 2/3: scarica video da R2, chiama Gemini, restituisce metadati testuali."""
    job_id = body.job_id or str(uuid.uuid4())[:8]

    tmp_dir  = tempfile.mkdtemp()
    filepath = os.path.join(tmp_dir, body.safe_filename)

    try:
        logger.info(f"[STEP/METADATA] Download da R2: {body.r2_video_url}")
        try:
            download_video(body.r2_video_url, filepath)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Download R2 fallito: {e}")

        try:
            meta = generate_metadata(filepath, filename=body.safe_filename)
        except GeminiGenerationError as e:
            raise HTTPException(status_code=e.http_status, detail=e.to_n8n_detail())
        except IncompleteGeminiMetadataError as e:
            raise HTTPException(status_code=422, detail={"error": "parse_error", "detail": str(e)})
        except Exception as e:
            logger.exception("[STEP/METADATA] Errore Gemini non classificato")
            raise HTTPException(status_code=502, detail={"error": "gemini_error", "detail": str(e)})

        logger.info(f"[STEP/METADATA] Metadati ricevuti: {meta}")

        if not meta.get("yt_title", "").strip() or not meta.get("thumbnail_text", "").strip():
            raise HTTPException(
                status_code=422,
                detail={"error": "parse_error", "detail": "Gemini ha restituito metadati incompleti (titolo o thumbnail_text vuoto)"},
            )

        return {
            "job_id":        job_id,
            "safe_filename": body.safe_filename,
            "r2_video_url":  body.r2_video_url,
            "r2_video_key":  body.r2_video_key,
            **meta
        }
    finally:
        try:
            os.remove(filepath)
            os.rmdir(tmp_dir)
        except Exception:
            pass


@app.post("/step/thumbnail", dependencies=[Depends(verify_api_key)])
def step_thumbnail(body: StepThumbnailRequest):
    """Passo 3/3: scarica video da R2, genera thumbnail con ffmpeg+Pillow, carica su R2."""
    job_id = body.job_id or str(uuid.uuid4())[:8]

    tmp_dir  = tempfile.mkdtemp()
    filepath = os.path.join(tmp_dir, body.safe_filename)

    try:
        logger.info(f"[STEP/THUMBNAIL] Download da R2: {body.r2_video_url}")
        try:
            download_video(body.r2_video_url, filepath)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Download R2 fallito: {e}")

        thumb_text = (
            body.thumbnail_text
            or body.safe_filename.replace("_", " ").replace(".mp4", "")
        )

        thumb_path = generate_thumbnail(filepath, thumb_text)

        if not thumb_path:
            raise HTTPException(status_code=500, detail="Generazione thumbnail fallita: nessun file prodotto")

        try:
            thumb_r2_key = body.r2_thumb_key or f"thumbnails/{job_id}/thumb.jpg"
            thumb_r2_url = upload_to_r2(thumb_path, thumb_r2_key)
            logger.info(f"[STEP/THUMBNAIL] Thumbnail R2: {thumb_r2_url}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Upload thumbnail R2 fallito: {e}")
        finally:
            try:
                os.remove(thumb_path)
            except Exception:
                pass

        return {
            "job_id":        job_id,
            "safe_filename": body.safe_filename,
            "r2_video_url":  body.r2_video_url,
            "r2_video_key":  body.r2_video_key,
            "r2_thumb_url":  thumb_r2_url,
            "r2_thumb_key":  thumb_r2_key,
            "thumbnail_text": thumb_text,
        }
    finally:
        try:
            os.remove(filepath)
            os.rmdir(tmp_dir)
        except Exception:
            pass


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

    res["platform"] = "youtube"
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
    res["platform"] = "facebook"
    return res


@app.post("/publish/instagram", dependencies=[Depends(verify_api_key)])
def publish_instagram(body: PublishInstagramRequest):
    """Controlla requisiti IG, pubblica Reel. Se file troppo grande o formato sbagliato, converte e riprova."""
    tmp_dir  = tempfile.mkdtemp()
    filename = body.safe_filename or "video.mp4"
    filepath = os.path.join(tmp_dir, filename)

    try:
        download_video(body.r2_video_url, filepath)
    except Exception as e:
        return {"success": False, "error": f"Download R2 fallito: {e}", "platform": "instagram"}

    check = check_instagram_requirements(filepath)
    logger.info(f"[IG CHECK] {check}")

    video_url = body.r2_video_url
    retried   = False

    if not check["ok"]:
        errors  = check["errors"]
        err_str = "; ".join(errors)
        needs_fix = any(
            k in err_str.lower()
            for k in ["troppo grande", "codec", "h.264", "aac", "formato", "hevc", "h265"]
        )

        if needs_fix:
            logger.info(f"[IG] Problemi: {err_str} — converto con ffmpeg")
            converted_path = os.path.join(tmp_dir, "converted.mp4")

            try:
                info     = get_video_info(filepath)
                duration = float(info.get("format", {}).get("duration", 60))
            except Exception:
                duration = 60.0

            target_mb   = 92.0
            target_kbps = int((target_mb * 8 * 1024) / duration)
            video_kbps  = max(500, target_kbps - 128)

            cmd = [
                "ffmpeg", "-y", "-i", filepath,
                "-c:v", "libx264", "-preset", "fast", "-b:v", f"{video_kbps}k",
                "-c:a", "aac", "-b:a", "128k",
                converted_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                return {
                    "success": False,
                    "error": f"Requisiti IG non soddisfatti: {err_str}. Conversione fallita: {result.stderr[-300:]}",
                    "platform": "instagram"
                }

            check2 = check_instagram_requirements(converted_path)
            if not check2["ok"]:
                return {
                    "success": False,
                    "error": f"Requisiti IG non soddisfatti anche dopo conversione: {'; '.join(check2['errors'])}",
                    "platform": "instagram"
                }

            r2_key        = f"videos/{uuid.uuid4().hex[:8]}/{filename}"
            r2_public_url = os.environ.get("R2_PUBLIC_URL", "")
            upload_to_r2(converted_path, r2_key)
            video_url = f"{r2_public_url}/{r2_key}"
            retried   = True
            logger.info(f"[IG] Re-uploadato convertito: {video_url}")
        else:
            try:
                os.remove(filepath)
                os.rmdir(tmp_dir)
            except Exception:
                pass
            return {"success": False, "error": f"Requisiti IG non soddisfatti: {err_str}", "platform": "instagram"}

    try:
        os.remove(filepath)
        os.rmdir(tmp_dir)
    except Exception:
        pass

    caption = body.ig_caption or filename.replace("_", " ").replace(".mp4", "")
    res = upload_reel(video_url, caption, cover_url=body.r2_thumb_url)
    logger.info(f"[IG] {res}")
    res["platform"] = "instagram"
    res["retried"]  = retried
    return res


@app.post("/compress-for-ig", dependencies=[Depends(verify_api_key)])
def compress_for_ig(body: CompressForIGRequest):
    """
    Scarica video da R2, lo reencoda in h264/aac sotto max_size_mb,
    sovrascrive la stessa chiave R2 con la versione compressa.
    Ritorna gli stessi r2_video_url e r2_video_key (ora puntano al file compresso).
    """
    job_id   = str(uuid.uuid4())[:8]
    tmp_dir  = tempfile.mkdtemp()
    in_path  = os.path.join(tmp_dir, f"src_{job_id}.mp4")
    out_path = os.path.join(tmp_dir, f"ig_{job_id}.mp4")

    try:
        # Scarica da R2
        try:
            download_video(body.r2_video_url, in_path)
        except Exception as e:
            return {"success": False, "error": f"Download fallito: {e}"}

        original_mb = os.path.getsize(in_path) / (1024 * 1024)
        logger.info(f"[COMPRESS] File originale: {original_mb:.1f}MB")

        # Ottieni durata per calcolare il bitrate target
        try:
            info     = get_video_info(in_path)
            duration = float(info.get("format", {}).get("duration", 0))
        except Exception as e:
            return {"success": False, "error": f"ffprobe fallito: {e}"}

        if duration <= 0:
            return {"success": False, "error": "Durata video non rilevabile"}

        # Bitrate target garantisce output < max_size_mb
        audio_kbps = 128
        target_total_kbps = int((body.max_size_mb * 8 * 1024) / duration)
        video_kbps = min(target_total_kbps - audio_kbps, 8000)  # cap 8 Mbps

        if video_kbps < 300:
            return {
                "success": False,
                "error": f"Video troppo lungo ({duration:.0f}s) per comprimere a {body.max_size_mb}MB con qualità accettabile (bitrate risultante: {video_kbps}kbps)"
            }

        logger.info(f"[COMPRESS] duration={duration:.1f}s → video_bitrate={video_kbps}kbps, audio={audio_kbps}kbps")

        cmd = [
            "ffmpeg", "-y",
            "-i", in_path,
            "-c:v", "libx264",
            "-b:v", f"{video_kbps}k",
            "-maxrate", f"{video_kbps}k",
            "-bufsize", f"{video_kbps * 2}k",
            "-preset", "fast",
            "-c:a", "aac",
            "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart",
            out_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            logger.error(f"[COMPRESS] ffmpeg stderr: {result.stderr[-1000:]}")
            return {"success": False, "error": f"ffmpeg fallito: {result.stderr[-400:]}"}

        compressed_mb = os.path.getsize(out_path) / (1024 * 1024)
        logger.info(f"[COMPRESS] Compresso: {original_mb:.1f}MB → {compressed_mb:.1f}MB")

        # Sovrascrive la stessa chiave R2 — cleanup esistente funziona senza modifiche
        try:
            r2_url = upload_to_r2(out_path, body.r2_video_key)
        except Exception as e:
            return {"success": False, "error": f"Upload R2 fallito: {e}"}

        return {
            "success":           True,
            "r2_video_url":      r2_url,
            "r2_video_key":      body.r2_video_key,
            "original_size_mb":  round(original_mb, 1),
            "compressed_size_mb": round(compressed_mb, 1),
            "video_bitrate_kbps": video_kbps,
        }

    finally:
        for p in [in_path, out_path]:
            try:
                os.remove(p)
            except Exception:
                pass
        try:
            os.rmdir(tmp_dir)
        except Exception:
            pass


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
