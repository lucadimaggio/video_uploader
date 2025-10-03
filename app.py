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
        url = f"https://graph.facebook.com/v21.0/{FB_PAGE_ID}/videos"
        files = {"file": open(filename, "rb")}
        payload = {
            "title": data.title,
            "description": data.description,
            "access_token": META_ACCESS_TOKEN
        }

        res = requests.post(url, data=payload, files=files)
        os.remove(filename)

        if res.status_code != 200:
            logger.error(f"Errore API Facebook: {res.text}")
            return make_response("error", "facebook", error=res.text)

        video_id = res.json().get("id")

        # Recupera il permalink reale
        url_permalink = f"https://graph.facebook.com/v21.0/{video_id}"
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

        return make_response("success", "facebook", link=link, publishAt=data.publishDate)

    except Exception as e:
        logger.exception("Errore imprevisto durante upload Facebook")
        return make_response("error", "facebook", error=str(e))


@app.post("/upload/instagram")
def upload_instagram(data: VideoData):
    try:
        logger.info(f"Inizio upload Instagram: titolo='{data.title}', url='{data.fileUrl}'")

        # Step 1: creazione media
        url_create = f"https://graph.facebook.com/v21.0/{IG_ACCOUNT_ID}/media"
        payload = {
            "video_url": data.fileUrl,
            "caption": data.description,
            "access_token": META_ACCESS_TOKEN
        }
        res_create = requests.post(url_create, data=payload)
        if res_create.status_code != 200:
            logger.error(f"Errore creazione media IG: {res_create.text}")
            return make_response("error", "instagram", error=res_create.text)

        creation_id = res_create.json().get("id")

        # Step 2: pubblicazione
        url_publish = f"https://graph.facebook.com/v21.0/{IG_ACCOUNT_ID}/media_publish"
        payload_pub = {
            "creation_id": creation_id,
            "access_token": META_ACCESS_TOKEN
        }
        res_pub = requests.post(url_publish, data=payload_pub)
        if res_pub.status_code != 200:
            logger.error(f"Errore pubblicazione IG: {res_pub.text}")
            return make_response("error", "instagram", error=res_pub.text)

        post_id = res_pub.json().get("id")

        # Recupera il permalink reale
        url_permalink = f"https://graph.facebook.com/v21.0/{post_id}"
        params = {
            "fields": "permalink",
            "access_token": META_ACCESS_TOKEN
        }
        res_link = requests.get(url_permalink, params=params)
        if res_link.status_code != 200:
            logger.error(f"Errore recupero permalink IG: {res_link.text}")
            return make_response("error", "instagram", error=res_link.text)

        link = res_link.json().get("permalink")
        logger.info(f"Video pubblicato su Instagram: id={post_id}, link={link}")

        return make_response("success", "instagram", link=link, publishAt=data.publishDate)

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
