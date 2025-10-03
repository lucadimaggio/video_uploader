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

META_ACCESS_TOKEN = os.environ["META_ACCESS_TOKEN"]
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

class VideoData(BaseModel):
    fileUrl: str
    title: str
    description: str
    publishDate: str
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
            "access_token": META_ACCESS_TOKEN
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
            "access_token": META_ACCESS_TOKEN
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
        logger.info(f"Inizio upload Instagram (Resumable): titolo='{data.title}', url='{data.fileUrl}'")

        # Scarica il file in locale
        r = requests.get(data.fileUrl, stream=True)
        if r.status_code != 200:
            logger.error(f"Errore download file: status={r.status_code}")
            return make_response("error", "instagram", error=f"Errore download: HTTP {r.status_code}")

        filename = "temp_ig_video.mp4"
        with open(filename, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

        file_size = os.path.getsize(filename)
        file_name = os.path.basename(filename)
        file_type = "video/mp4"

        # Step 1: start upload session
        url_start = f"https://graph.facebook.com/v23.0/{META_APP_ID}/uploads"
        payload_start = {
            "file_name": file_name,
            "file_length": file_size,
            "file_type": file_type,
            "access_token": META_ACCESS_TOKEN
        }
        res_start = requests.post(url_start, data=payload_start)
        if res_start.status_code != 200:
            os.remove(filename)
            error_msg = res_start.json().get("error", {}).get("message", res_start.text)
            logger.error(f"Errore start upload IG: {error_msg}")
            return make_response("error", "instagram", error=error_msg)

        upload_session_id = res_start.json().get("id")

        # Step 2: upload file (single shot se piccolo, chunk se grande)
        url_upload = f"https://graph.facebook.com/v23.0/{upload_session_id}"
        headers = {
            "Authorization": f"OAuth {META_ACCESS_TOKEN}",
            "file_offset": "0"
        }
        with open(filename, "rb") as f:
            file_data = f.read()

        res_upload = requests.post(url_upload, headers=headers, data=file_data)
        if res_upload.status_code != 200:
            os.remove(filename)
            error_msg = res_upload.json().get("error", {}).get("message", res_upload.text)
            logger.error(f"Errore upload IG: {error_msg}")
            return make_response("error", "instagram", error=error_msg)

        file_handle = res_upload.json().get("h")

        # Step 3: crea media object con file handle
        url_media = f"https://graph.facebook.com/v23.0/{IG_ACCOUNT_ID}/media"
        payload_media = {
            "video": file_handle,
            "caption": data.description,
            "access_token": META_ACCESS_TOKEN
        }
        res_media = requests.post(url_media, data=payload_media)
        if res_media.status_code != 200:
            os.remove(filename)
            error_msg = res_media.json().get("error", {}).get("message", res_media.text)
            logger.error(f"Errore creazione media IG: {error_msg}")
            return make_response("error", "instagram", error=error_msg)

        creation_id = res_media.json().get("id")

        # Step 4: pubblica il video
        url_publish = f"https://graph.facebook.com/v23.0/{IG_ACCOUNT_ID}/media_publish"
        payload_pub = {
            "creation_id": creation_id,
            "access_token": META_ACCESS_TOKEN
        }
        res_pub = requests.post(url_publish, data=payload_pub)
        os.remove(filename)

        if res_pub.status_code != 200:
            error_msg = res_pub.json().get("error", {}).get("message", res_pub.text)
            logger.error(f"Errore pubblicazione IG: {error_msg}")
            return make_response("error", "instagram", error=error_msg)

        post_id = res_pub.json().get("id")

        # Step 5: recupera permalink
        url_permalink = f"https://graph.facebook.com/v23.0/{post_id}"
        params = {
            "fields": "permalink",
            "access_token": META_ACCESS_TOKEN
        }
        res_link = requests.get(url_permalink, params=params)
        if res_link.status_code != 200:
            error_msg = res_link.json().get("error", {}).get("message", res_link.text)
            logger.error(f"Errore recupero permalink IG: {error_msg}")
            return make_response("error", "instagram", error=error_msg)

        link = res_link.json().get("permalink")
        logger.info(f"Video pubblicato su Instagram: id={post_id}, link={link}")

        return make_response("success", "instagram", link=link, publishAt=data.publishDate or None)

    except Exception as e:
        logger.exception("Errore imprevisto durante upload Instagram")
        return make_response("error", "instagram", error=str(e))




@app.post("/upload/youtube")
def upload_youtube(data: VideoData):
    try:
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
                "title": data.title,
                "description": data.description,
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
