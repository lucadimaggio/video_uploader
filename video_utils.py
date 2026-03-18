"""
video_utils.py — Sanitize filename + check requisiti Instagram Reels
Richiede: ffprobe installato (ffmpeg suite)
"""
import re
import subprocess
import json
import os
import logging
import unicodedata

logger = logging.getLogger(__name__)

IG_MAX_SIZE_MB  = 100
IG_MAX_DURATION = 90    # secondi
IG_VIDEO_CODEC  = "h264"
IG_AUDIO_CODEC  = "aac"


def sanitize_filename(name: str) -> str:
    """
    Rimuove caratteri speciali, unicode, emoji, accenti dal filename.
    Es: "Caso Studio - €200k 🔥.mp4" → "Caso_Studio_200k.mp4"
    """
    base, ext = os.path.splitext(name)
    base = unicodedata.normalize("NFKD", base)
    base = base.encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[\s\-]+", "_", base)
    base = re.sub(r"[^a-zA-Z0-9_]", "", base)
    base = base[:60].strip("_")
    return base + ext.lower()


def get_video_info(filepath: str) -> dict:
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        filepath
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe error: {result.stderr}")
    return json.loads(result.stdout)


def check_instagram_requirements(filepath: str) -> dict:
    """
    Verifica requisiti IG Reels.
    Ritorna: {"ok": bool, "errors": [...], "warnings": [...], "info": {...}}
    """
    errors   = []
    warnings = []
    info     = {}

    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    info["size_mb"] = round(size_mb, 2)
    if size_mb > IG_MAX_SIZE_MB:
        errors.append(f"File troppo grande: {size_mb:.1f}MB (max {IG_MAX_SIZE_MB}MB)")

    try:
        probe = get_video_info(filepath)
    except RuntimeError as e:
        errors.append(str(e))
        return {"ok": False, "errors": errors, "warnings": warnings, "info": info}

    streams  = probe.get("streams", [])
    fmt      = probe.get("format", {})

    duration = float(fmt.get("duration", 0))
    info["duration_s"] = round(duration, 2)
    if duration > IG_MAX_DURATION:
        errors.append(f"Durata {duration:.1f}s supera il limite di {IG_MAX_DURATION}s")

    video_stream = next((s for s in streams if s["codec_type"] == "video"), None)
    audio_stream = next((s for s in streams if s["codec_type"] == "audio"), None)

    if video_stream:
        vcodec = video_stream.get("codec_name", "unknown")
        info["video_codec"] = vcodec
        if vcodec != IG_VIDEO_CODEC:
            errors.append(f"Codec video '{vcodec}' non supportato (richiesto h264)")
        w = video_stream.get("width", 0)
        h = video_stream.get("height", 0)
        info["resolution"] = f"{w}x{h}"
        if w > 0 and h > 0 and abs((h / w) - (16 / 9)) > 0.05:
            warnings.append(f"Aspect ratio {w}x{h} non è 9:16 (consigliato per Reels)")
    else:
        errors.append("Nessun stream video trovato")

    if audio_stream:
        acodec = audio_stream.get("codec_name", "unknown")
        info["audio_codec"] = acodec
        if acodec != IG_AUDIO_CODEC:
            errors.append(f"Codec audio '{acodec}' non supportato (richiesto aac)")
    else:
        warnings.append("Nessun stream audio trovato")

    return {"ok": len(errors) == 0, "errors": errors, "warnings": warnings, "info": info}
