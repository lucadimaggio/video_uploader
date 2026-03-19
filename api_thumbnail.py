"""
api_thumbnail.py — Genera thumbnail dal frame migliore del video.
Usa ffmpeg per estrarre il frame, Pillow per il testo sovrapposto.
"""
import os
import subprocess
import tempfile
import logging
from PIL import Image, ImageDraw, ImageFont

logger    = logging.getLogger(__name__)
FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "Inter-Black.ttf")


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

    PADDING      = 48
    MAX_W        = W - PADDING * 2
    font_size    = 148
    font         = ImageFont.truetype(FONT_PATH, font_size)
    line_spacing = 16
    space_w      = draw.textbbox((0, 0), " ", font=font)[2]

    # Word wrap (max 3 parole per riga)
    words   = text.upper().split()
    lines   = []
    current = []
    for word in words:
        test = " ".join(current + [word])
        bw   = draw.textbbox((0, 0), test, font=font)[2]
        if len(current) >= 3 or (current and bw > MAX_W):
            lines.append(current)
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(current)

    line_h  = draw.textbbox((0, 0), "A", font=font)[3]
    total_h = len(lines) * line_h + (len(lines) - 1) * line_spacing
    text_y  = int(H * 0.68) - total_h // 2

    for line in lines:
        line_w = sum(draw.textbbox((0, 0), w, font=font)[2] for w in line) + space_w * (len(line) - 1)
        x = (W - line_w) // 2
        for word in line:
            for dx, dy in [(-3,0),(3,0),(0,-3),(0,3),(-3,-3),(3,-3),(-3,3),(3,3)]:
                draw.text((x+dx, text_y+dy), word, font=font, fill=(0, 0, 0))
            draw.text((x, text_y), word, font=font, fill="white")
            x += draw.textbbox((0, 0), word, font=font)[2] + space_w
        text_y += line_h + line_spacing

    img.save(output_path, "JPEG", quality=95)
    logger.info(f"[THUMBNAIL] Salvata: {output_path}")
