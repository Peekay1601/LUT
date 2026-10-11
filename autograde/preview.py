"""Contact sheets and before/after previews (for humans and for Claude)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .media import signal_to_display_image


def _font(size: int = 16):
    for name in ("DejaVuSans.ttf", "Arial.ttf", "Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _thumb(sig: np.ndarray, width: int) -> Image.Image:
    img = signal_to_display_image(sig)
    h = int(round(img.height * width / img.width))
    return img.resize((width, h), Image.Resampling.LANCZOS)


def contact_sheet(rows: list[tuple[str, list[tuple[str, np.ndarray]]]], path: Path, thumb_w: int = 240) -> Path:
    """Grid: one row per shot (label, [(caption, signal), ...])."""
    font, small = _font(16), _font(12)
    label_w, pad, cap_h = 150, 6, 16
    cols = max((len(f) for _, f in rows), default=1)
    sample_h = max((int(round(f[0][1].shape[0] * thumb_w / f[0][1].shape[1])) for _, f in rows if f), default=135)
    row_h = sample_h + cap_h + pad
    sheet = Image.new("RGB", (label_w + cols * (thumb_w + pad) + pad, pad + len(rows) * row_h), (24, 24, 24))
    d = ImageDraw.Draw(sheet)
    for r, (label, frames) in enumerate(rows):
        y = pad + r * row_h
        d.text((pad, y + sample_h // 2 - 8), label, fill=(230, 230, 230), font=font)
        for c, (cap, sig) in enumerate(frames):
            x = label_w + c * (thumb_w + pad)
            t = _thumb(sig, thumb_w)
            sheet.paste(t, (x, y))
            d.text((x + 2, y + t.height + 1), cap, fill=(170, 170, 170), font=small)
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, quality=92)
    return path


def before_after(before: np.ndarray, after: np.ndarray, path: Path, width: int = 640) -> Path:
    a, b = _thumb(before, width), _thumb(after, width)
    out = Image.new("RGB", (a.width * 2 + 6, a.height + 22), (24, 24, 24))
    out.paste(a, (0, 22))
    out.paste(b, (a.width + 6, 22))
    d = ImageDraw.Draw(out)
    d.text((6, 3), "before", fill=(220, 220, 220), font=_font(14))
    d.text((a.width + 12, 3), "after", fill=(220, 220, 220), font=_font(14))
    path.parent.mkdir(parents=True, exist_ok=True)
    out.save(path, quality=92)
    return path


def save_signal(sig: np.ndarray, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    signal_to_display_image(sig).save(path)
    return path
