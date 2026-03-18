"""
app.py — FastAPI server (Railway)
Flusso opzione B (approvazione manuale):
  1. n8n chiama POST /preview
  2. FastAPI scarica video, genera metadati Gemini, uploda su R2, manda Telegram con bottoni
  3. Luca approva → POST /telegram/webhook → FastAPI pubblica su YT/FB/IG
"""
import os
import json
import uuid
import logging
import tempfile
import requests as req_lib

from fastapi import FastAPI, HTTPException, Header, Depends, Request
from pydantic import BaseModel, field_validator

from video_utils import sanitize_filename, check_instagram_requirements
from api_instagram import upload_reel
from api_youtube import upload_video as yt_upload
from api_facebook import upload_video as fb_upload
from api_r2 import upload_to_r2, delete_from_r2
from api_gemini import generate_metadata

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Video Uploader")

TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
INTERNAL_API_KEY    = os.environ.get("INTERNAL_API_KEY", "")
R2_PUBLIC_URL       = os.environ.get("R2_PUBLIC_URL", "")
N8N_DELETE_WEBHOOK  = os.environ.get("N8N_DELETE_WEBHOOK", "")  # webhook n8n per eliminare da Drive

# Job store in memoria: job_id -> job dict
# Contiene r2_key, r2_url, metadata, request body
_jobs: dict[str, dict] = {}


# ── Auth ──────────────────────────────────────────────────────────────────────

def verify_api_key(x_api_key: str = Header(...)):
    if INTERNAL_API_KEY and x_api_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Models ────────────────────────────────────────────────────────────────────

class PreviewRequest(BaseModel):
    video_url:     str
    filename:      str
    drive_file_id: str = ""   # ID file su Google Drive (per eliminazione post-publish)
    platforms:     list[str] = ["youtube", "facebook", "instagram"]

    @field_validator("platforms", mode="before")
    @classmethod
    def parse_platforms(cls, v):
        if isinstance(v, str):
            return json.loads(v)
        return v


# ── Telegram helpers ──────────────────────────────────────────────────────────

def _tg(method: str, payload: dict):
    try:
        r = req_lib.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
            json=payload, timeout=10
        )
        return r.json()
    except Exception as e:
        logger.warning(f"[TELEGRAM] {method} fallito: {e}")
        return {}


def send_preview(job_id: str, safe_name: str, meta: dict):
    """Manda messaggio Telegram con metadati e bottoni approva/annulla."""
    text = (
        f"📹 *Nuovo video pronto per la pubblicazione*\n"
        f"File: `{safe_name}`\n\n"
        f"*Titolo YouTube:*\n{meta.get('yt_title', '—')}\n\n"
        f"*Descrizione YouTube:*\n{meta.get('yt_description', '—')}\n\n"
        f"*Caption Instagram:*\n{meta.get('ig_caption', '—')}\n\n"
        f"*Facebook:*\n{meta.get('fb_description', '—')}\n\n"
        f"Approvi la pubblicazione?"
    )
    _tg("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Approva e pubblica", "callback_data": f"approve_{job_id}"},
                {"text": "❌ Annulla",            "callback_data": f"cancel_{job_id}"}
            ]]
        }
    })


def notify_telegram(message: str):
    _tg("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    })


def delete_from_drive(file_id: str):
    """Chiama il webhook n8n per eliminare il file da Google Drive."""
    if not N8N_DELETE_WEBHOOK or not file_id:
        return
    try:
        req_lib.post(N8N_DELETE_WEBHOOK, json={"file_id": file_id}, timeout=10)
        logger.info(f"[DRIVE] Richiesta eliminazione file {file_id} inviata a n8n")
    except Exception as e:
        logger.warning(f"[DRIVE] Eliminazione fallita: {e}")


def answer_callback(callback_id: str, text: str):
    _tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def edit_message_text(chat_id: int, message_id: int, text: str):
    _tg("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {"inline_keyboard": []}
    })


# ── Download helper ───────────────────────────────────────────────────────────

def download_video(url: str, dest_path: str):
    logger.info(f"[DOWNLOAD] {url}")
    r = req_lib.get(url, stream=True, timeout=180)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)


# ── Publish pipeline ──────────────────────────────────────────────────────────

def run_publish(job: dict) -> dict:
    """
    Esegue la pubblicazione su tutte le piattaforme.
    Scarica da R2 per YT (che richiede file locale), usa URL R2 per FB/IG.
    """
    meta      = job["meta"]
    r2_url    = job["r2_url"]
    r2_key    = job["r2_key"]
    safe_name = job["safe_name"]
    platforms = job["platforms"]
    results   = {}

    # Scarica da R2 per YouTube
    tmp_dir  = tempfile.mkdtemp()
    filepath = os.path.join(tmp_dir, safe_name)

    try:
        download_video(r2_url, filepath)
    except Exception as e:
        raise RuntimeError(f"Download da R2 fallito: {e}")

    # YouTube
    if "youtube" in platforms:
        res = yt_upload(
            filepath,
            title=meta.get("yt_title") or safe_name.replace("_", " ").replace(".mp4", ""),
            description=meta.get("yt_description") or "",
            privacy="public"
        )
        results["youtube"] = res
        logger.info(f"[YT] {res}")

    # Facebook
    if "facebook" in platforms:
        res = fb_upload(
            r2_url,
            description=meta.get("fb_description") or safe_name.replace("_", " ").replace(".mp4", "")
        )
        results["facebook"] = res
        logger.info(f"[FB] {res}")

    # Instagram
    if "instagram" in platforms:
        check = check_instagram_requirements(filepath)
        logger.info(f"[IG CHECK] {check}")

        if not check["ok"]:
            err_str = "; ".join(check["errors"])
            results["instagram"] = {"success": False, "error": err_str}
            notify_telegram(
                f"*IG Upload BLOCCATO*\nFile: `{safe_name}`\nErrori: {err_str}"
            )
            logger.warning(f"[IG] Bloccato — file R2 conservato: {r2_key}")
            r2_key = None  # Non eliminare
        else:
            ig_cap = meta.get("ig_caption") or safe_name.replace("_", " ").replace(".mp4", "")
            res = upload_reel(r2_url, ig_cap)
            results["instagram"] = res
            logger.info(f"[IG] {res}")

            if not res["success"]:
                notify_telegram(
                    f"*IG Upload FALLITO*\nFile: `{safe_name}`\n"
                    f"Errore: {res.get('error')}\n"
                    f"Dettagli: `{json.dumps(res.get('details', {}), ensure_ascii=False)[:300]}`"
                )
                logger.warning(f"[IG] Fallito — file R2 conservato: {r2_key}")
                r2_key = None

    # Cleanup locale
    try:
        os.remove(filepath)
        os.rmdir(tmp_dir)
    except Exception:
        pass

    # Cleanup R2 (solo se tutto ok)
    if r2_key:
        try:
            delete_from_r2(r2_key)
        except Exception as e:
            logger.warning(f"[R2] Cleanup fallito: {e}")

    return results


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/preview", dependencies=[Depends(verify_api_key)])
def preview(body: PreviewRequest):
    """
    1. Scarica video
    2. Genera metadati con Gemini
    3. Uploda su R2
    4. Manda Telegram con bottoni approva/annulla
    """
    safe_name = sanitize_filename(body.filename)
    job_id    = str(uuid.uuid4())[:8]

    # Download
    tmp_dir  = tempfile.mkdtemp()
    filepath = os.path.join(tmp_dir, safe_name)
    try:
        download_video(body.video_url, filepath)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download fallito: {e}")

    logger.info(f"[PREVIEW] File pronto: {filepath} ({os.path.getsize(filepath)/1024/1024:.1f}MB)")

    # Genera metadati Gemini
    meta = generate_metadata(filepath)
    logger.info(f"[PREVIEW] Metadati: {meta}")

    # Upload su R2 (video resta lì fino ad approvazione)
    try:
        r2_key = f"pending/{job_id}/{safe_name}"
        r2_url = upload_to_r2(filepath, r2_key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload R2 fallito: {e}")

    # Cleanup locale (il video è su R2)
    try:
        os.remove(filepath)
        os.rmdir(tmp_dir)
    except Exception:
        pass

    # Salva job in memoria
    _jobs[job_id] = {
        "job_id":        job_id,
        "safe_name":     safe_name,
        "r2_key":        r2_key,
        "r2_url":        r2_url,
        "meta":          meta,
        "platforms":     body.platforms,
        "drive_file_id": body.drive_file_id,
    }

    # Manda Telegram
    send_preview(job_id, safe_name, meta)
    logger.info(f"[PREVIEW] Job {job_id} in attesa di approvazione Telegram")

    return {"job_id": job_id, "status": "awaiting_approval", "meta": meta}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Riceve callback dai bottoni inline Telegram."""
    data = await request.json()
    logger.info(f"[WEBHOOK] {json.dumps(data)[:300]}")

    callback = data.get("callback_query")
    if not callback:
        return {"ok": True}

    callback_id = callback["id"]
    chat_id     = callback["message"]["chat"]["id"]
    message_id  = callback["message"]["message_id"]
    cb_data     = callback.get("data", "")

    if cb_data.startswith("approve_"):
        job_id = cb_data.replace("approve_", "")
        job    = _jobs.get(job_id)

        if not job:
            answer_callback(callback_id, "Job non trovato o scaduto.")
            return {"ok": True}

        answer_callback(callback_id, "Pubblicazione in corso...")
        edit_message_text(chat_id, message_id, f"⏳ Pubblicazione `{job['safe_name']}` in corso...")

        try:
            results  = run_publish(job)
            del _jobs[job_id]

            yt_ok = "✅" if results.get("youtube", {}).get("success") else "❌"
            fb_ok = "✅" if results.get("facebook", {}).get("success") else "❌"
            ig_ok = "✅" if results.get("instagram", {}).get("success") else "❌"

            all_ok = all([
                results.get("youtube", {}).get("success", True),
                results.get("facebook", {}).get("success", True),
                results.get("instagram", {}).get("success", True),
            ])

            # Elimina da Drive solo se tutto pubblicato correttamente
            if all_ok and job.get("drive_file_id"):
                delete_from_drive(job["drive_file_id"])
                drive_note = "\n🗑️ File eliminato da Drive"
            else:
                drive_note = "\n⚠️ File Drive conservato (errori su alcune piattaforme)"

            edit_message_text(chat_id, message_id,
                f"✅ *Pubblicazione completata*\n"
                f"File: `{job['safe_name']}`\n\n"
                f"YouTube {yt_ok} | Facebook {fb_ok} | Instagram {ig_ok}"
                f"{drive_note}"
            )
        except Exception as e:
            logger.error(f"[PUBLISH] Errore: {e}")
            edit_message_text(chat_id, message_id, f"❌ Errore durante la pubblicazione:\n`{e}`")

    elif cb_data.startswith("cancel_"):
        job_id = cb_data.replace("cancel_", "")
        job    = _jobs.pop(job_id, None)

        if job:
            try:
                delete_from_r2(job["r2_key"])
            except Exception:
                pass

        answer_callback(callback_id, "Annullato.")
        edit_message_text(chat_id, message_id, f"❌ Pubblicazione annullata.")

    return {"ok": True}
