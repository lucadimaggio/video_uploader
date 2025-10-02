from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests
import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

YOUTUBE_CLIENT_ID = os.environ["YOUTUBE_CLIENT_ID"]
YOUTUBE_CLIENT_SECRET = os.environ["YOUTUBE_CLIENT_SECRET"]
YOUTUBE_REFRESH_TOKEN = os.environ["YOUTUBE_REFRESH_TOKEN"]

def make_response(status: str, platform: str, link: str = None, error: str = None):
    return {
        "status": status,
        "platform": platform,
        "link": link,
        "error": error
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

@app.post("/upload/youtube")
def upload_youtube(data: VideoData):
    try:
        # 1. Scarica il file dal link Google Drive
        r = requests.get(data.fileUrl, stream=True)
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail="Errore download file")
        
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
        video_id = response.get("id")
        video_link = f"https://www.youtube.com/watch?v={video_id}"

        print(f"Carico video {filename} con titolo {data.title}")

        # 3. Rimuovo il file temporaneo
        os.remove(filename)
        return make_response("success", "youtube", link=video_link)



    except HttpError as e:
        return make_response("error", "youtube", error=f"HTTP error: {e}")
    except Exception as e:
        return make_response("error", "youtube", error=str(e))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
