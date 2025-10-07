from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests
import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

import logging

# Configura logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


YOUTUBE_CLIENT_ID = os.environ["YOUTUBE_CLIENT_ID"]
YOUTUBE_CLIENT_SECRET = os.environ["YOUTUBE_CLIENT_SECRET"]
YOUTUBE_REFRESH_TOKEN = os.environ["YOUTUBE_REFRESH_TOKEN"]

META_IG_TOKEN = os.environ["META_IG_TOKEN"]
META_FB_TOKEN = os.environ["META_FB_TOKEN"]
FB_PAGE_ID = os.environ["FB_PAGE_ID"]
IG_ACCOUNT_ID = os.environ["IG_ACCOUNT_ID"]
META_APP_ID = os.environ["META_APP_ID"]


def make_response(status: str, platform: str, link: str = None, error: str = None, publishAt: str = None):
    return {
        "status": status,
        "platform": platform,
        "link": link,
        "error": error,
        "publishAt": publishAt
    }


def get_youtube_service():
    creds = Credentials(
        None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token"
    )
    return build("youtube", "v3", credentials=creds)


app = FastAPI()

# Monta una cartella pubblica per i video temporanei
from fastapi.staticfiles import StaticFiles
os.makedirs("videos", exist_ok=True)
app.mount("/videos", StaticFiles(directory="videos"), name="videos")
logger.info("Cartella pubblica '/videos' pronta per servire file temporanei.")


class VideoData(BaseModel):
    fileUrl: str
    title: str | None = None
    description: str | None = None
    publishDate: str | None = None

@app.get("/auth/tiktok/callback")
def tiktok_callback(code: str = None, state: str = None):
    """
    Endpoint di callback per TikTok OAuth.
    Riceve il 'code' dopo l'autenticazione e restituisce una conferma.
    """
    if not code:
        logger.warning("Richiesta di callback TikTok senza 'code'")
        return {"error": "missing_code"}
    logger.info(f"Ricevuto TikTok OAuth code: {code}")
    return {"status": "ok", "code": code}


@app.post("/upload/facebook")
def upload_facebook(data: VideoData):
    try:
        logger.info(f"Inizio upload Facebook: titolo='{data.title}', url='{data.fileUrl}'")

        # Scarica file da Drive
        r = requests.get(data.fileUrl, stream=True)
        if r.status_code != 200:
            logger.error(f"Errore download file Drive: status={r.status_code}")
            return make_response("error", "facebook", error=f"Errore download: HTTP {r.status_code}")

        filename = "temp_fb_video.mp4"
        with open(filename, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

        # Upload su Facebook
        url = f"https://graph.facebook.com/v23.0/{FB_PAGE_ID}/videos"
        files = {"file": open(filename, "rb")}
        payload = {
            "title": data.title,
            "description": data.description,
            "access_token": META_FB_TOKEN
        }

        res = requests.post(url, data=payload, files=files)
        os.remove(filename)

        if res.status_code != 200:
            try:
                error_msg = res.json().get("error", {}).get("message", res.text)
            except Exception:
                error_msg = res.text
            logger.error(f"Errore API Facebook: {error_msg}")
            return make_response("error", "facebook", error=error_msg)

        video_id = res.json().get("id")

        # Recupera il permalink reale
        url_permalink = f"https://graph.facebook.com/v23.0/{video_id}"
        params = {
            "fields": "permalink_url",
            "access_token": META_FB_TOKEN
        }
        res_link = requests.get(url_permalink, params=params)
        if res_link.status_code != 200:
            logger.error(f"Errore recupero permalink FB: {res_link.text}")
            return make_response("error", "facebook", error=res_link.text)

        link = res_link.json().get("permalink_url")
        logger.info(f"Video caricato su Facebook: id={video_id}, link={link}")

        return make_response("success", "facebook", link=link, publishAt=data.publishDate or None)

    except Exception as e:
        logger.exception("Errore imprevisto durante upload Facebook")
        return make_response("error", "facebook", error=str(e))


@app.post("/upload/instagram")
def upload_instagram(data: VideoData):
    try:
        logger.info(f"Inizio upload Instagram (REELS): titolo='{data.title}', url='{data.fileUrl}'")

        # Scarica il file da Drive e salvalo nella cartella pubblica
        os.makedirs("videos", exist_ok=True)
        local_path = f"videos/{data.title}.mp4"
        r = requests.get(data.fileUrl, stream=True)
        if r.status_code != 200:
            logger.error(f"Errore download file: status={r.status_code}")
            return make_response("error", "instagram", error=f"Errore download: HTTP {r.status_code}")

        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

        # Costruisci link pubblico temporaneo
        server_url = os.environ.get("RAILWAY_STATIC_URL", "https://tuo-progetto.up.railway.app")
        video_url = f"https://{server_url.replace('https://', '').replace('http://', '')}/videos/{os.path.basename(local_path).replace('.mp4.mp4', '.mp4')}"
        logger.info(f"Link temporaneo disponibile: {video_url}")

        # ✅ Carica il file su Cloudflare R2 per ottenere un link pubblico stabile
        import boto3

        r2_client = boto3.client(
            "s3",
            endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto"
        )

        bucket_name = os.environ["R2_BUCKET_NAME"]
        object_key = os.path.basename(local_path)
        logger.info(f"Caricamento video su Cloudflare R2: {object_key}")

        with open(local_path, "rb") as f:
            r2_client.upload_fileobj(
                f,
                bucket_name,
                object_key,
                ExtraArgs={"ACL": "public-read", "ContentType": "video/mp4"}
            )

        # Costruisci link pubblico diretto (usando R2_PUBLIC_URL impostato su Railway)
        base_url = os.environ.get("R2_PUBLIC_URL", f"https://pub-{os.environ['R2_ACCOUNT_ID']}.r2.dev")
        video_url = f"{base_url.rstrip('/')}/{object_key}"
        logger.info(f"Link pubblico R2 pronto: {video_url}")

        # Crea media object su Instagram come REEL
        url_media = f"https://graph.facebook.com/v23.0/{IG_ACCOUNT_ID}/media"
        payload_media = {
            "video_url": video_url,
            "caption": data.description,
            "media_type": "REELS",
            "access_token": META_IG_TOKEN
        }
        res_media = requests.post(url_media, data=payload_media)
        if res_media.status_code != 200:
            error_msg = res_media.json().get("error", {}).get("message", res_media.text)
            logger.error(f"Errore creazione media IG: {error_msg}")
            return make_response("error", "instagram", error=error_msg)

        creation_id = res_media.json().get("id")
        logger.info(f"Media object creato correttamente: creation_id={creation_id}")

        # Attendi che Instagram completi l'elaborazione del video
        import time
        status_url = f"https://graph.facebook.com/v23.0/{creation_id}?fields=status_code&access_token={META_IG_TOKEN}"

        for i in range(30):  # controllo ogni 2 secondi per massimo ~60 secondi
            res_status = requests.get(status_url)
            if res_status.status_code == 200:
                status = res_status.json().get("status_code")
                logger.info(f"Stato corrente del video: {status}")
                if status == "FINISHED":
                    logger.info("Elaborazione video completata, procedo alla pubblicazione.")
                    break
                elif status == "ERROR":
                    logger.error("Errore durante l'elaborazione video Instagram.")
                    return make_response("error", "instagram", error="Elaborazione video fallita su Instagram.")
            time.sleep(2)
        else:
            logger.error("Timeout: video non elaborato entro 60 secondi.")
            return make_response("error", "instagram", error="Timeout elaborazione video Instagram.")



        # Pubblica il Reel
        url_publish = f"https://graph.facebook.com/v23.0/{IG_ACCOUNT_ID}/media_publish"
        payload_pub = {
            "creation_id": creation_id,
            "access_token": META_IG_TOKEN
        }
        res_pub = requests.post(url_publish, data=payload_pub)

        if res_pub.status_code != 200:
            error_msg = res_pub.json().get("error", {}).get("message", res_pub.text)
            logger.error(f"Errore pubblicazione IG: {error_msg}")
            return make_response("error", "instagram", error=error_msg)

        post_id = res_pub.json().get("id")

        # Recupera il permalink
        url_permalink = f"https://graph.facebook.com/v23.0/{post_id}"
        params = {"fields": "permalink", "access_token": META_IG_TOKEN}
        res_link = requests.get(url_permalink, params=params)
        if res_link.status_code != 200:
            error_msg = res_link.json().get("error", {}).get("message", res_link.text)
            logger.error(f"Errore recupero permalink IG: {error_msg}")
            return make_response("error", "instagram", error=error_msg)

        link = res_link.json().get("permalink")
        logger.info(f"Reel pubblicato con successo: {link}")

        # 🔄 Elimina il file dal bucket R2 dopo la pubblicazione
        try:
            r2_client.delete_object(Bucket=bucket_name, Key=object_key)
            logger.info(f"File {object_key} eliminato da Cloudflare R2.")
        except Exception as e:
            logger.warning(f"Impossibile eliminare file da R2: {e}")


        # Rimozione del file dopo la pubblicazione
        try:
            os.remove(local_path)
            logger.info("File temporaneo rimosso.")
        except Exception as e:
            logger.warning(f"Impossibile rimuovere file temporaneo: {e}")

        return make_response("success", "instagram", link=link, publishAt=data.publishDate or None)

    except Exception as e:
        logger.exception("Errore imprevisto durante upload Instagram")
        return make_response("error", "instagram", error=str(e))



@app.post("/upload/youtube")
def upload_youtube(data: VideoData):
    try:
                # Controllo titolo
        if not data.title or data.title.strip() == "":
            logger.warning("Titolo non fornito. Imposto titolo di default.")
            data.title = "Video automatico"

        # 1. Scarica il file dal link Google Drive
        logger.info(f"Inizio upload video: titolo='{data.title}', url='{data.fileUrl}'")

        r = requests.get(data.fileUrl, stream=True)
        if r.status_code != 200:
            logger.error(f"Errore nel download file da Drive: status={r.status_code}")
            return make_response("error", "youtube", error=f"Errore download file: HTTP {r.status_code}")

        filename = "temp_video.mp4"
        with open(filename, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

        # 2. Upload reale a YouTube
        youtube = get_youtube_service()

        body = {
            "snippet": {
                "title": data.description or "Video automatico",
                "description": data.description or "",
                "categoryId": "22"  # categoria generica "People & Blogs"
            },
            "status": {
                "privacyStatus": "private"  # oppure "public", "unlisted"
            }
        }

        # Se data.publishDate è futura → programmazione
        if data.publishDate:
            body["status"]["publishAt"] = data.publishDate

        media = MediaFileUpload(filename, chunksize=-1, resumable=True)

        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media
        )

        response = request.execute()
        publish_at = response.get("status", {}).get("publishAt")
        video_id = response.get("id")
        video_link = f"https://www.youtube.com/watch?v={video_id}"

        logger.info(f"Video caricato correttamente: id={video_id}, titolo='{data.title}'")

        # 3. Rimuovo il file temporaneo
        os.remove(filename)
        return make_response("success", "youtube", link=video_link, publishAt=publish_at)



    except HttpError as e:
        logger.error(f"Errore API YouTube: {e}")
        return make_response("error", "youtube", error=f"Errore API YouTube: {str(e)}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Errore di rete durante il download: {e}")
        return make_response("error", "youtube", error=f"Errore rete download: {str(e)}")
    except Exception as e:
        logger.exception("Errore imprevisto durante upload")
        return make_response("error", "youtube", error=f"Errore generico: {str(e)}")

@app.post("/upload/tiktok")
def upload_tiktok(data: VideoData):
    """
    Endpoint per l'upload su TikTok (sandbox o produzione).
    Attualmente restituisce una risposta simulata per testare la pipeline.
    """
    try:
        logger.info(f"Inizio upload TikTok: titolo='{data.title}', url='{data.fileUrl}'")

        # Qui in futuro: scambio code -> access_token e chiamata alle API TikTok
        # Al momento simuliamo la risposta di successo
        fake_link = "https://www.tiktok.com/@me/video/1234567890"
        logger.info(f"Simulazione completata. TikTok link: {fake_link}")

        return make_response("success", "tiktok", link=fake_link, publishAt=data.publishDate or None)

    except Exception as e:
        logger.exception("Errore durante upload TikTok")
        return make_response("error", "tiktok", error=str(e))

        return make_response("error", "tiktok", error=str(e))


@app.get("/refresh/meta-token")
def refresh_meta_token():
    """
    Endpoint per rigenerare automaticamente i token long-lived di Meta (Facebook + Instagram).
    Può essere chiamato da n8n o manualmente tramite browser.
    """
    try:
        logger.info("Inizio refresh token Meta...")

        META_APP_ID = os.environ["META_APP_ID"]
        META_APP_SECRET = os.environ["META_APP_SECRET"]
        META_FB_TOKEN = os.environ["META_FB_TOKEN"]

        # Richiesta al Graph API per generare un nuovo token long-lived
        refresh_url = (
            f"https://graph.facebook.com/v23.0/oauth/access_token?"
            f"grant_type=fb_exchange_token&client_id={META_APP_ID}&"
            f"client_secret={META_APP_SECRET}&fb_exchange_token={META_FB_TOKEN}"
        )

        res = requests.get(refresh_url)
        if res.status_code != 200:
            logger.error(f"Errore durante il refresh token Meta: {res.text}")
            return make_response("error", "meta", error=res.text)

        data = res.json()
        new_token = data.get("access_token")
        expires_in = data.get("expires_in", 0)

        logger.info(f"Nuovo token Meta ottenuto. Scade tra {expires_in/86400:.1f} giorni.")

        # Facoltativo: stampa in console il token (solo per debugging)
        logger.info(f"Nuovo META_FB_TOKEN: {new_token}")

        # Restituisce il nuovo token come risposta
        return make_response("success", "meta", link=None, error=None, publishAt=None) | {
            "new_token": new_token,
            "expires_in": expires_in
        }

    except Exception as e:
        logger.exception("Errore durante il refresh del token Meta.")
        return make_response("error", "meta", error=str(e))



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)


