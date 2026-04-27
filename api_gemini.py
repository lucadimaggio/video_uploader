"""
api_gemini.py — Genera metadati automatici dal video usando Gemini 2.0 Flash.
Carica il video sulla Gemini File API, poi chiede titolo/descrizione/caption.
"""
import os
import re
import time
import json
import logging
import google.generativeai as genai

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


class IncompleteGeminiMetadataError(RuntimeError):
    pass

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
  "yt_title": "titolo YouTube forte e specifico, 55-75 caratteri. Studia il contenuto reale del video e scegli l'angolo piu sorprendente, concreto o contro-intuitivo. Usa numeri precisi se emergono dal video (es. '47 clienti', '3x ROAS', '30 giorni'). Crea curiosity gap oppure enuncia il beneficio principale in modo diretto e netto. VIETATO: titoli vaghi, generici o motivazionali come 'Consigli per E-Commerce', 'Come migliorare le vendite', 'Strategia vincente'. Esempio buono: 'Come questo brand Shopify ha triplicato le conversioni in 45 giorni' — Esempio cattivo: 'Come aumentare le vendite online'.",
  "yt_description": "descrizione YouTube informativa e sobria, max 200 caratteri",
  "ig_caption": "caption Instagram diretta e professionale + 5 hashtag rilevanti per ecommerce e business italiano, max 250 caratteri totali",
  "fb_description": "descrizione Facebook sobria e diretta, max 200 caratteri",
  "thumbnail_text": "headline molto breve per copertina YouTube, ideale 2-4 parole, massimo 5 parole. Deve essere specifica rispetto al punto chiave del video, non generica, non motivazionale, non da template. Evita formule banali come '3 consigli', 'errori da evitare', 'come fare', 'guida completa', 'strategia vincente' se non sono davvero il cuore del contenuto mostrato. Scegli l'angolo piu forte, concreto o sorprendente emerso nel video e trasformalo in poche parole nette, impattanti e riconoscibili. Preferisci parole corte e facili da leggere grandi su thumbnail, usa sinonimi piu brevi quando possibile, elimina nomi inutili e dettagli secondari."
}"""


TITLE_QUALITY_PROMPT = """
Istruzione aggiuntiva per yt_title:
Genera un titolo YouTube in italiano, 40-70 caratteri, specifico, curiosity-driven e SEO-friendly.
Evita titoli generici o riassunti banali come "analisi target", "video marketing" o "come fare X".
Il titolo deve anticipare l'insight o il risultato principale mostrato nel video, con voce attiva e senza clickbait.
Deve sembrare qualcosa che un professionista marketing cliccherebbe.
"""


def sanitize_prompt_filename(filename: str) -> str:
    base, ext = os.path.splitext(filename or "")
    base = re.sub(r"[-_()]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip()
    return f"{base}{ext}" if base else (ext.lstrip(".") or "video.mp4")


def _normalize_metadata_keys(metadata: dict) -> dict:
    if not metadata.get("yt_title") and metadata.get("title"):
        metadata["yt_title"] = metadata.get("title", "")
    if not metadata.get("thumbnail_text"):
        metadata["thumbnail_text"] = metadata.get("thumbnailtext", "")
    return metadata


def _has_required_metadata(metadata: dict) -> bool:
    return bool(
        metadata.get("yt_title", "").strip()
        and metadata.get("thumbnail_text", "").strip()
    )


def generate_metadata(video_path: str, filename: str = "") -> dict:
    """
    Carica il video su Gemini File API e genera i metadati.
    Restituisce dict con yt_title, yt_description, ig_caption, fb_description.
    In caso di errore ritenta automaticamente; se fallisce sempre, solleva RuntimeError.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("Gemini: API key mancante")

    genai.configure(api_key=GEMINI_API_KEY)
    retry_delay = 2
    last_error = None
    prompt_filename = sanitize_prompt_filename(filename or os.path.basename(video_path))
    prompt = (
        f"Nome file sanitizzato da usare solo come contesto, non come titolo automatico: {prompt_filename}\n\n"
        + PROMPT
        + TITLE_QUALITY_PROMPT
    )

    for attempt in range(1, 4):
        video_file = None
        logger.info(f"[GEMINI] Tentativo {attempt}/3...")

        try:
            # 1. Upload file su Gemini File API (fresco a ogni tentativo)
            logger.info(f"[GEMINI] Upload video: {video_path}")
            video_file = genai.upload_file(path=video_path, mime_type="video/mp4")

            # 2. Attendi che il file sia processato
            logger.info("[GEMINI] Attendo elaborazione file...")
            for _ in range(30):
                video_file = genai.get_file(video_file.name)
                if video_file.state.name == "ACTIVE":
                    break
                if video_file.state.name == "FAILED":
                    raise RuntimeError("Elaborazione file fallita")
                time.sleep(3)
            else:
                raise RuntimeError("Timeout elaborazione file")

            # 3. Genera metadati
            logger.info("[GEMINI] Generazione metadati...")
            model = genai.GenerativeModel("gemini-2.0-flash")
            response = model.generate_content([video_file, prompt])
            raw = response.text.strip()
            logger.info(f"[GEMINI] Risposta raw: {raw[:200]}")

            # Pulizia eventuale markdown
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            metadata = _normalize_metadata_keys(json.loads(raw))
            logger.info(f"[GEMINI] Metadati generati: {metadata}")
            if not _has_required_metadata(metadata):
                missing = []
                if not metadata.get("yt_title", "").strip():
                    missing.append("yt_title")
                if not metadata.get("thumbnail_text", "").strip():
                    missing.append("thumbnail_text")
                raise IncompleteGeminiMetadataError(
                    f"Gemini ha restituito metadati incompleti: {', '.join(missing)}"
                )
            return metadata

        except Exception as e:
            last_error = e
            logger.error(f"[GEMINI] Tentativo {attempt}/3 fallito: {e}")
            if attempt < 3:
                logger.info(f"[GEMINI] Nuovo tentativo tra {retry_delay}s")
                time.sleep(retry_delay)
        finally:
            # Elimina il file da Gemini per non accumulare storage
            if video_file and getattr(video_file, "name", None):
                try:
                    genai.delete_file(video_file.name)
                except Exception:
                    pass

    if isinstance(last_error, IncompleteGeminiMetadataError):
        raise IncompleteGeminiMetadataError(
            "Gemini ha restituito metadati incompleti dopo 3 tentativi"
        ) from last_error
    raise RuntimeError("Gemini: generazione metadati fallita dopo 3 tentativi") from last_error

def _empty() -> dict:
    return {
        "yt_title": "",
        "yt_description": "",
        "ig_caption": "",
        "fb_description": "",
        "thumbnail_text": ""
    }
