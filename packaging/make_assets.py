"""Generate panel assets: brand mark and look thumbnails rendered by the real engine.

Usage: python packaging/make_assets.py   (also run by build_ccx.py)
"""

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autograde import colour  # noqa: E402
from autograde.grade import LOOKS, apply_grade  # noqa: E402

ASSETS = ROOT / "premiere-panel" / "assets"


def mark(size: int = 108) -> Image.Image:
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    r = int(s * 0.27)
    for (cx, cy), col in (((0.37, 0.39), (232, 98, 84)), ((0.63, 0.39), (72, 196, 120)), ((0.5, 0.63), (82, 136, 255))):
        circle = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(circle).ellipse([cx * s - r, cy * s - r, cx * s + r, cy * s + r], fill=col + (255,))
        # Additive-looking overlap: screen blend the circles.
        a = np.asarray(layer).astype(np.float32) / 255
        b = np.asarray(circle).astype(np.float32) / 255
        rgb = 1 - (1 - a[..., :3] * a[..., 3:]) * (1 - b[..., :3] * b[..., 3:])
        alpha = np.maximum(a[..., 3:], b[..., 3:])
        out = np.concatenate([np.where(alpha > 0, rgb / np.maximum(alpha, 1e-6), 0), alpha], axis=-1)
        layer = Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8), "RGBA")
    img.alpha_composite(layer)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def scene(w: int = 400, h: int = 176) -> np.ndarray:
    """A small graded-friendly scene in scene-linear light: sky, sun, hills, skin, neutrals."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    v = yy / h
    img = np.zeros((h, w, 3), np.float32)
    # Sky: deep blue at the top to a warm horizon.
    t = np.clip(v / 0.6, 0, 1)[..., None]
    img[:] = (1 - t) * np.array([0.10, 0.20, 0.55]) + t * np.array([0.95, 0.62, 0.38])
    # Sun glow.
    d = np.hypot(xx - w * 0.72, yy - h * 0.48) / h
    img += np.exp(-(d / 0.18) ** 2)[..., None] * np.array([1.4, 1.0, 0.55])
    # Far hills (cool) and near hills (green).
    far = yy > h * (0.55 + 0.05 * np.sin(xx / w * 7.0))
    img[far] = np.array([0.16, 0.20, 0.26])
    near = yy > h * (0.68 + 0.07 * np.sin(xx / w * 4.0 + 1.3))
    img[near] = (np.array([0.05, 0.13, 0.04]) * (0.7 + 0.6 * (1 - v[near]))[..., None])
    # A figure: skin face and a grey jacket.
    face = (xx - w * 0.30) ** 2 / (h * 0.09) ** 2 + (yy - h * 0.52) ** 2 / (h * 0.12) ** 2 < 1
    body = (np.abs(xx - w * 0.30) < h * 0.16) & (yy > h * 0.63)
    img[body] = np.array([0.16, 0.16, 0.16])
    img[face] = np.array([0.55, 0.36, 0.26])
    # Neutral chart strip bottom-right.
    for i, g in enumerate((0.05, 0.18, 0.45, 0.85)):
        x0 = int(w * 0.80) + i * int(w * 0.045)
        img[int(h * 0.82):int(h * 0.94), x0:x0 + int(w * 0.04)] = g
    return np.clip(img, 0, None)


def main() -> None:
    (ASSETS / "looks").mkdir(parents=True, exist_ok=True)
    mark(108).save(ASSETS / "mark.png")
    sig = colour.oetf_709(scene())
    sig = np.clip(sig, 0, 1)
    for name, look in LOOKS.items():
        out = apply_grade(sig, None, look)
        Image.fromarray((out * 255 + 0.5).astype(np.uint8)).resize((200, 88), Image.Resampling.LANCZOS).save(ASSETS / "looks" / f"{name}.png")
    print(f"assets written to {ASSETS}")


if __name__ == "__main__":
    main()
