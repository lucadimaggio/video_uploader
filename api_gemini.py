"""
api_gemini.py — Genera metadati automatici dal video usando Gemini 2.0 Flash.
Carica il video sulla Gemini File API, poi chiede titolo/descrizione/caption.
"""
import os
import time
import json
import logging
import google.generativeai as genai

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

PROMPT = """Guarda questo video. È un contenuto per i social media di BaseForce,
un'azienda italiana di consulenza strategica per brand e-commerce su Shopify.

Basandoti su ciò che viene detto e mostrato nel video, genera in italiano i testi per la pubblicazione.

Regole tassative:
- Niente emoji
- Niente punti esclamativi
- Tono professionale, diretto, mai markettaro o entusiasta
- Non invitare a scrivere in DM, non invitare a contattare
- Scrivi sempre "E-Commerce" (maiuscolo, con trattino) e mai "e-commerce" o "ecommerce"
- In ogni testo (YouTube, Instagram, Facebook) inserisci sempre questa frase: "Sul nostro canale YouTube trovi casi studio di E-Commerce reali che fatturano 50k al mese o piu."

Rispondi SOLO con JSON valido, senza markdown, senza spiegazioni:
{
  "yt_title": "titolo descrittivo e chiaro, max 60 caratteri",
  "yt_description": "descrizione YouTube informativa e sobria, max 200 caratteri",
  "ig_caption": "caption Instagram diretta e professionale + 5 hashtag rilevanti per ecommerce e business italiano, max 250 caratteri totali",
  "fb_description": "descrizione Facebook sobria e diretta, max 200 caratteri",
  "thumbnail_text": "headline molto breve per copertina YouTube, ideale 2-4 parole, massimo 5 parole. Deve essere specifica rispetto al punto chiave del video, non generica, non motivazionale, non da template. Evita formule banali come '3 consigli', 'errori da evitare', 'come fare', 'guida completa', 'strategia vincente' se non sono davvero il cuore del contenuto mostrato. Scegli l'angolo piu forte, concreto o sorprendente emerso nel video e trasformalo in poche parole nette, impattanti e riconoscibili. Preferisci parole corte e facili da leggere grandi su thumbnail, usa sinonimi piu brevi quando possibile, elimina nomi inutili e dettagli secondari."
}"""


def generate_metadata(video_path: str) -> dict:
    """
    Carica il video su Gemini File API e genera i metadati.
    Restituisce dict con yt_title, yt_description, ig_caption, fb_description.
    In caso di errore restituisce dict con valori vuoti.
    """
    if not GEMINI_API_KEY:
        logger.warning("[GEMINI] API key mancante — metadati non generati")
        return _empty()

    genai.configure(api_key=GEMINI_API_KEY)

    # 1. Upload file su Gemini File API
    logger.info(f"[GEMINI] Upload video: {video_path}")
    try:
        video_file = genai.upload_file(path=video_path, mime_type="video/mp4")
    except Exception as e:
        logger.error(f"[GEMINI] Upload fallito: {e}")
        return _empty()

    # 2. Attendi che il file sia processato
    logger.info("[GEMINI] Attendo elaborazione file...")
    for _ in range(30):
        video_file = genai.get_file(video_file.name)
        if video_file.state.name == "ACTIVE":
            break
        if video_file.state.name == "FAILED":
            logger.error("[GEMINI] Elaborazione file fallita")
            return _empty()
        time.sleep(3)
    else:
        logger.error("[GEMINI] Timeout elaborazione file")
        return _empty()

    # 3. Genera metadati
    logger.info("[GEMINI] Generazione metadati...")
    try:
        model = genai.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content([video_file, PROMPT])
        raw = response.text.strip()
        logger.info(f"[GEMINI] Risposta raw: {raw[:200]}")

        # Pulizia eventuale markdown
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        metadata = json.loads(raw)
        logger.info(f"[GEMINI] Metadati generati: {metadata}")
        return metadata

    except Exception as e:
        logger.error(f"[GEMINI] Generazione fallita: {e}")
        return _empty()

    finally:
        # Elimina il file da Gemini per non accumulare storage
        try:
            genai.delete_file(video_file.name)
        except Exception:
            pass


def _empty() -> dict:
    return {
        "yt_title": "",
        "yt_description": "",
        "ig_caption": "",
        "fb_description": "",
        "thumbnail_text": ""
    }
