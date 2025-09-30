from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests
import os

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

        # 2. Qui andrebbe l'upload alle API di YouTube
        # TODO: integrare con Google API client
        print(f"Carico video {filename} con titolo {data.title}")

        # 3. Rimuovo il file temporaneo
        os.remove(filename)

        return {
            "status": "success",
            "platform": "youtube",
            "link": "https://youtube.com/watch?v=dummy123"  # link mock
        }

    except Exception as e:
        return {"status": "error", "error": str(e)}
