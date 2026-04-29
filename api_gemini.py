"""
api_gemini.py — Genera metadati automatici dal video usando Gemini 2.0 Flash.
Carica il video sulla Gemini File API, poi chiede titolo/descrizione/caption.
"""
import os
import re
import time
import json
import logging
import random
import google.generativeai as genai

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "120"))
GEMINI_MAX_ATTEMPTS = int(os.environ.get("GEMINI_MAX_ATTEMPTS", "5"))


class IncompleteGeminiMetadataError(RuntimeError):
    pass


class GeminiGenerationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "gemini_error",
        http_status: int = 502,
        retryable: bool = True,
        fallback_allowed: bool = True,
        retry_after_seconds: int | None = None,
        model: str | None = None,
        original_error: Exception | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.http_status = http_status
        self.retryable = retryable
        self.fallback_allowed = fallback_allowed
        self.retry_after_seconds = retry_after_seconds
        self.model = model
        self.original_error = original_error

    def to_n8n_detail(self) -> dict:
        detail = {"error": self.error_code, "detail": str(self)}
        if self.retry_after_seconds is not None:
            detail["retry_after_seconds"] = self.retry_after_seconds
        if self.model:
            detail["model"] = self.model
        return detail

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


def _sanitize_log_text(value: object, max_chars: int = 300) -> str:
    text = str(value or "")
    if GEMINI_API_KEY:
        text = text.replace(GEMINI_API_KEY, "***GEMINI_API_KEY***")
    text = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "***GEMINI_API_KEY***", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _exception_status_code(exc: Exception) -> int | None:
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
        if hasattr(value, "value") and isinstance(value.value, int):
            return value.value
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code if isinstance(status_code, int) else None


def _exception_response_preview(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if response is None:
        return ""
    body = getattr(response, "text", None) or getattr(response, "content", None) or ""
    return _sanitize_log_text(body, 200)


def _classify_gemini_error(exc: Exception, *, model: str) -> GeminiGenerationError:
    if isinstance(exc, GeminiGenerationError):
        return exc
    if isinstance(exc, IncompleteGeminiMetadataError):
        return GeminiGenerationError(
            str(exc),
            error_code="parse_error",
            http_status=502,
            retryable=True,
            fallback_allowed=True,
            model=model,
            original_error=exc,
        )

    status_code = _exception_status_code(exc)
    name = type(exc).__name__
    message = _sanitize_log_text(exc, 300)
    haystack = f"{name} {message}".lower()

    if status_code == 429 or "resourceexhausted" in haystack or "quota" in haystack or "rate limit" in haystack:
        return GeminiGenerationError(
            "Quota o rate limit Gemini raggiunto",
            error_code="rate_limit",
            http_status=429,
            retryable=True,
            fallback_allowed=True,
            retry_after_seconds=60,
            model=model,
            original_error=exc,
        )
    if status_code in (401, 403) or "permissiondenied" in haystack or "api key" in haystack or "unauth" in haystack:
        return GeminiGenerationError(
            "API key Gemini non valida o assente",
            error_code="auth_failed",
            http_status=401,
            retryable=False,
            fallback_allowed=False,
            model=model,
            original_error=exc,
        )
    if status_code == 404 or "notfound" in haystack or "not found" in haystack:
        return GeminiGenerationError(
            "Modello Gemini non disponibile o nome errato",
            error_code="model_unavailable",
            http_status=422,
            retryable=False,
            fallback_allowed=False,
            model=model,
            original_error=exc,
        )
    if status_code == 400 or "invalidargument" in haystack or "invalid argument" in haystack:
        return GeminiGenerationError(
            message or "Input Gemini non valido",
            error_code="invalid_input",
            http_status=422,
            retryable=False,
            fallback_allowed=False,
            model=model,
            original_error=exc,
        )
    if "deadlineexceeded" in haystack or "timeout" in haystack or "timed out" in haystack:
        return GeminiGenerationError(
            "Timeout chiamata Gemini",
            error_code="timeout",
            http_status=504,
            retryable=True,
            fallback_allowed=True,
            model=model,
            original_error=exc,
        )
    if status_code and 500 <= status_code < 600:
        return GeminiGenerationError(
            message or "Errore 5xx Gemini",
            error_code="gemini_error",
            http_status=502,
            retryable=True,
            fallback_allowed=True,
            model=model,
            original_error=exc,
        )
    return GeminiGenerationError(
        message or "Errore Gemini non classificato",
        error_code="gemini_error",
        http_status=502,
        retryable=True,
        fallback_allowed=True,
        model=model,
        original_error=exc,
    )


def _log_gemini_failure(exc: Exception, *, attempt: int, model: str, prompt: str, duration_ms: int):
    status_code = _exception_status_code(exc)
    logger.error(
        "[GEMINI] failure attempt=%s model=%s prompt_chars=%s call_duration_ms=%s "
        "exception_type=%s exception_message=%s gemini_status_code=%s gemini_response_preview=%s",
        attempt,
        model,
        len(prompt),
        duration_ms,
        type(exc).__name__,
        _sanitize_log_text(exc, 300),
        status_code,
        _exception_response_preview(exc),
    )


def validate_gemini_config(ping: bool = False) -> None:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY mancante o vuota")
    if not GEMINI_MODEL:
        raise RuntimeError("GEMINI_MODEL mancante o vuota")
    if GEMINI_TIMEOUT <= 0:
        raise RuntimeError("GEMINI_TIMEOUT deve essere maggiore di zero")
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info(
        "[GEMINI] config ok model=%s timeout=%ss",
        GEMINI_MODEL,
        GEMINI_TIMEOUT,
    )
    if ping:
        start = time.perf_counter()
        response = genai.GenerativeModel(GEMINI_MODEL).generate_content(
            "Rispondi solo OK",
            request_options={"timeout": GEMINI_TIMEOUT},
        )
        logger.info("[GEMINI] ping ok call_duration_ms=%s", int((time.perf_counter() - start) * 1000))


def _parse_metadata_response(response, *, model_name: str) -> dict:
    finish_reason = ""
    try:
        finish_reason = str(response.candidates[0].finish_reason)
    except Exception:
        pass
    if "SAFETY" in finish_reason.upper():
        raise GeminiGenerationError(
            "Gemini ha bloccato la risposta per safety",
            error_code="safety_block",
            http_status=422,
            retryable=False,
            fallback_allowed=False,
            model=model_name,
        )

    raw = (response.text or "").strip()
    logger.info("[GEMINI] Risposta raw preview: %s", _sanitize_log_text(raw, 200))
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        metadata = _normalize_metadata_keys(json.loads(raw))
    except Exception as exc:
        raise GeminiGenerationError(
            "Risposta Gemini non parsabile come JSON",
            error_code="parse_error",
            http_status=502,
            retryable=True,
            fallback_allowed=True,
            model=model_name,
            original_error=exc,
        ) from exc

    logger.info("[GEMINI] Metadati generati: %s", metadata)
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


def _generate_metadata_with_model(video_path: str, prompt: str, *, model_name: str) -> dict:
    last_error = None
    attempts = max(1, GEMINI_MAX_ATTEMPTS)

    for attempt in range(1, attempts + 1):
        video_file = None
        started = time.perf_counter()
        logger.info(
            "[GEMINI] Tentativo %s/%s model=%s prompt_chars=%s",
            attempt,
            attempts,
            model_name,
            len(prompt),
        )

        try:
            logger.info("[GEMINI] Upload video: %s", video_path)
            video_file = genai.upload_file(path=video_path, mime_type="video/mp4")

            logger.info("[GEMINI] Attendo elaborazione file...")
            for _ in range(30):
                video_file = genai.get_file(video_file.name)
                if video_file.state.name == "ACTIVE":
                    break
                if video_file.state.name == "FAILED":
                    raise RuntimeError("Elaborazione file fallita")
                time.sleep(3)
            else:
                raise TimeoutError("Timeout elaborazione file")

            logger.info("[GEMINI] Generazione metadati...")
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(
                [video_file, prompt],
                request_options={"timeout": GEMINI_TIMEOUT},
            )
            metadata = _parse_metadata_response(response, model_name=model_name)
            logger.info(
                "[GEMINI] success attempt=%s model=%s prompt_chars=%s call_duration_ms=%s",
                attempt,
                model_name,
                len(prompt),
                int((time.perf_counter() - started) * 1000),
            )
            return metadata

        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            classified = _classify_gemini_error(exc, model=model_name)
            last_error = classified
            _log_gemini_failure(exc, attempt=attempt, model=model_name, prompt=prompt, duration_ms=duration_ms)
            if not classified.retryable or attempt >= attempts:
                raise classified from exc
            if classified.error_code == "rate_limit":
                delay = 60.0
                logger.warning(
                    "[GEMINI] rate_limit rilevato model=%s. Se persiste, aggiorna GEMINI_MODEL su Railway "
                    "con un modello stabile e disponibile per il tuo piano.",
                    model_name,
                )
            else:
                delay = min(1.5 * (2 ** (attempt - 1)) + random.uniform(0, 1), 30.0)
            logger.warning(
                "[GEMINI] retry attempt=%s/%s next_delay=%.1fs error=%s",
                attempt,
                attempts,
                delay,
                classified.error_code,
            )
            time.sleep(delay)
        finally:
            if video_file and getattr(video_file, "name", None):
                try:
                    genai.delete_file(video_file.name)
                except Exception as cleanup_error:
                    logger.warning("[GEMINI] Cleanup file fallito: %s", _sanitize_log_text(cleanup_error, 200))

    raise last_error or GeminiGenerationError("Gemini: generazione metadati fallita", model=model_name)


def generate_metadata(video_path: str, filename: str = "") -> dict:
    """
    Carica il video su Gemini File API e genera i metadati.
    Restituisce dict con yt_title, yt_description, ig_caption, fb_description.
    In caso di errore ritenta automaticamente; se fallisce sempre, solleva RuntimeError.
    """
    if not GEMINI_API_KEY:
        raise GeminiGenerationError(
            "API key Gemini non valida o assente",
            error_code="auth_failed",
            http_status=401,
            retryable=False,
            fallback_allowed=False,
        )

    genai.configure(api_key=GEMINI_API_KEY)
    prompt_filename = sanitize_prompt_filename(filename or os.path.basename(video_path))
    prompt = (
        f"Nome file sanitizzato da usare solo come contesto, non come titolo automatico: {prompt_filename}\n\n"
        + PROMPT
        + TITLE_QUALITY_PROMPT
    )

    return _generate_metadata_with_model(video_path, prompt, model_name=GEMINI_MODEL)
