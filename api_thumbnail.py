"""
api_thumbnail.py — Genera thumbnail dal frame migliore del video.
Usa ffmpeg per estrarre il frame, Pillow per il testo sovrapposto.
"""
import os
import re
import subprocess
import tempfile
import logging
from PIL import Image, ImageDraw, ImageFont

logger    = logging.getLogger(__name__)
FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "Inter-Black.ttf")
BOX_LEFT_RATIO = 0.08
BOX_RIGHT_RATIO = 0.92
BOX_TOP_RATIO = 0.46
BOX_BOTTOM_RATIO = 0.80
START_FONT_SIZE = 148
MIN_FONT_SIZE = 84
FONT_STEP = 6
MAX_LINES = 2
MAX_WORDS_PER_LINE = 3
STOPWORDS = {
    "A", "AD", "AL", "ALLA", "ALLE", "ALL'", "AI", "AGLI", "DA", "DAL", "DEI",
    "DEL", "DELLA", "DELLE", "DI", "E", "GLI", "IL", "IN", "LA", "LE", "LO",
    "NEI", "NEL", "PER", "SU", "TRA", "UNA", "UNO", "UN"
}
SHORT_WORD_REPLACEMENTS = {
    "AUMENTARE": "CRESCERE",
    "AUMENTA": "CRESCE",
    "OTTIMIZZARE": "MIGLIORARE",
    "OTTIMIZZA": "MIGLIORA",
    "OTTIMIZZAZIONE": "CRESCITA",
    "STRATEGICA": "SMART",
    "STRATEGICO": "SMART",
    "CONSULENZA": "GUIDA",
    "PERFORMANCE": "RISULTATI",
    "ACQUISIZIONE": "CLIENTI",
    "MARGINALITA": "MARGINI",
    "PROFITTABILITA": "PROFITTI",
}


def generate_thumbnail(video_path: str, text: str) -> str | None:
    """
    Estrae il frame migliore dal video e ci sovrappone il testo.
    Restituisce il path del file JPEG generato, o None in caso di errore.
    """
    tmp_dir    = tempfile.mkdtemp()
    frame_path = os.path.join(tmp_dir, "frame.jpg")
    thumb_path = os.path.join(tmp_dir, "thumbnail.jpg")

    # Estrai frame migliore con ffmpeg thumbnail filter
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", video_path,
            "-vf", "thumbnail=n=60", "-frames:v", "1",
            frame_path
        ], capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"[THUMBNAIL] Estrazione frame fallita: {e.stderr.decode()[:200]}")
        return None

    # Genera thumbnail con testo
    try:
        _render(frame_path, text, thumb_path)
        os.remove(frame_path)
        return thumb_path
    except Exception as e:
        logger.error(f"[THUMBNAIL] Rendering fallito: {e}")
        return None


def _render(frame_path: str, text: str, output_path: str):
    img  = Image.open(frame_path).convert("RGB")
    W, H = img.size

    # Overlay scuro graduale dal 38% inferiore
    overlay  = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ov_draw  = ImageDraw.Draw(overlay)
    grad_top = int(H * 0.38)
    for y in range(grad_top, H):
        alpha = int(220 * (y - grad_top) / (H - grad_top))
        ov_draw.rectangle([(0, y), (W, y)], fill=(0, 0, 0, alpha))
    img  = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    box = (
        int(W * BOX_LEFT_RATIO),
        int(H * BOX_TOP_RATIO),
        int(W * BOX_RIGHT_RATIO),
        int(H * BOX_BOTTOM_RATIO),
    )
    layout = _fit_text(draw, text, box)
    if not layout:
        logger.warning("[THUMBNAIL] Testo non fit: uso fallback minimale")
        layout = _fit_text(draw, "GUARDA QUESTO VIDEO", box)
    if not layout:
        raise ValueError("Impossibile far rientrare il testo nella thumbnail")

    _, text_y, font, lines, line_spacing = layout
    line_h = _line_height(draw, font)

    for line in lines:
        line_w = _line_width(draw, line, font)
        x = (W - line_w) // 2
        for word in line:
            word_w = _word_width(draw, word, font)
            outline = max(2, font.size // 40)
            for dx, dy in [
                (-outline, 0), (outline, 0), (0, -outline), (0, outline),
                (-outline, -outline), (outline, -outline),
                (-outline, outline), (outline, outline)
            ]:
                draw.text((x + dx, text_y + dy), word, font=font, fill=(0, 0, 0))
            draw.text((x, text_y), word, font=font, fill="white")
            x += word_w + _space_width(draw, font)
        text_y += line_h + line_spacing

    img.save(output_path, "JPEG", quality=95)
    logger.info(f"[THUMBNAIL] Salvata: {output_path}")


def _fit_text(draw: ImageDraw.ImageDraw, text: str, box: tuple[int, int, int, int]):
    candidates = _build_candidates(text)
    for font_size in range(START_FONT_SIZE, MIN_FONT_SIZE - 1, -FONT_STEP):
        font = ImageFont.truetype(FONT_PATH, font_size)
        for candidate in candidates:
            lines = _wrap_words(draw, candidate, font, box[2] - box[0])
            if not lines:
                continue
            line_spacing = max(10, font_size // 9)
            line_h = _line_height(draw, font)
            total_h = len(lines) * line_h + (len(lines) - 1) * line_spacing
            if total_h > (box[3] - box[1]):
                continue
            text_y = box[1] + ((box[3] - box[1]) - total_h) // 2
            logger.info(
                "[THUMBNAIL] Fit ok text='%s' font=%s lines=%s",
                candidate,
                font_size,
                len(lines),
            )
            return candidate, text_y, font, lines, line_spacing
    return None


def _build_candidates(text: str) -> list[str]:
    base = _normalize_text(text)
    if not base:
        return []

    words = base.split()
    candidates: list[str] = [base]
    if len(words) > 5:
        candidates.append(" ".join(words[:5]))
    if len(words) > 4:
        candidates.append(" ".join(words[:4]))

    compact_words = [SHORT_WORD_REPLACEMENTS.get(word, word) for word in words]
    compact = " ".join(compact_words)
    if compact and compact not in candidates:
        candidates.append(compact)

    no_stopwords = [word for word in compact_words if word not in STOPWORDS]
    if no_stopwords:
        reduced = " ".join(no_stopwords[:4])
        if reduced and reduced not in candidates:
            candidates.append(reduced)
        reduced_short = " ".join(no_stopwords[:3])
        if reduced_short and reduced_short not in candidates:
            candidates.append(reduced_short)

    first_three = " ".join(compact_words[:3])
    if first_three and first_three not in candidates:
        candidates.append(first_three)

    return candidates


def _normalize_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text.strip().upper())
    cleaned = re.sub(r"[^A-Z0-9À-ÖØ-Ý?' ]+", "", cleaned)
    return cleaned.strip()


def _wrap_words(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[list[str]]:
    words = text.split()
    if not words:
        return []

    for word in words:
        if _word_width(draw, word, font) > max_w:
            return []

    lines: list[list[str]] = []
    current: list[str] = []
    for word in words:
        proposed = current + [word]
        if len(proposed) > MAX_WORDS_PER_LINE:
            lines.append(current)
            current = [word]
        elif current and _line_width(draw, proposed, font) > max_w:
            lines.append(current)
            current = [word]
        else:
            current = proposed

        if len(lines) >= MAX_LINES and current:
            return []

    if current:
        lines.append(current)

    if len(lines) > MAX_LINES:
        return []
    return lines


def _word_width(draw: ImageDraw.ImageDraw, word: str, font: ImageFont.FreeTypeFont) -> int:
    bbox = draw.textbbox((0, 0), word, font=font)
    return bbox[2] - bbox[0]


def _space_width(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont) -> int:
    bbox = draw.textbbox((0, 0), " ", font=font)
    return bbox[2] - bbox[0]


def _line_width(draw: ImageDraw.ImageDraw, words: list[str], font: ImageFont.FreeTypeFont) -> int:
    return sum(_word_width(draw, word, font) for word in words) + _space_width(draw, font) * (len(words) - 1)


def _line_height(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont) -> int:
    bbox = draw.textbbox((0, 0), "AY", font=font)
    return bbox[3] - bbox[1]
