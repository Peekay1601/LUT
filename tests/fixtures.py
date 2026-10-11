"""Synthetic Rec.709 test footage with *known* exposure and colour errors.

Each clip renders the same kind of scene (grey cards, a skin-tone patch,
saturated objects, a gradient sky, a moving element) with a different framing,
then applies a known camera error in scene-linear light before encoding to
H.264 tagged as Rec.709 limited range.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from autograde import colour

W, H = 640, 360


def _scene(t: float, offset: int) -> np.ndarray:
    """Scene-linear RGB image of a simple 'set' (reflectances under neutral light)."""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    img = np.empty((H, W, 3), np.float32)
    # Background wall: warm-ish beige, with gentle falloff.
    img[:] = np.array([0.30, 0.27, 0.22]) * (0.75 + 0.25 * (1 - yy / H))[..., None]
    # Sky gradient across the top.
    sky = yy < H * 0.25
    img[sky] = (np.array([0.35, 0.50, 0.80]) * (0.8 + 0.2 * (yy[sky] / (H * 0.25)))[..., None])
    # Grey cards (true neutrals) at 9%, 18%, 45%, 80%.
    for i, v in enumerate((0.09, 0.18, 0.45, 0.80)):
        x0 = 40 + offset + i * 70
        img[200:300, x0:x0 + 60] = v
    # Skin-tone patch ("face").
    cx, cy = 430 - offset // 2, 210
    face = (xx - cx) ** 2 / 45 ** 2 + (yy - cy) ** 2 / 60 ** 2 < 1
    img[face] = np.array([0.46, 0.30, 0.22])
    # Saturated objects.
    img[110:170, 520 - offset:600 - offset] = [0.60, 0.06, 0.05]
    img[300:350, 520:620] = [0.05, 0.35, 0.08]
    # Moving element so frames differ over time.
    mx = int((t * 60 + offset * 3) % (W - 60))
    img[60:90, mx:mx + 60] = [0.20, 0.20, 0.55]
    # Mild texture/noise.
    rng = np.random.default_rng(int(t * 1000) + offset)
    img *= 1.0 + rng.normal(0, 0.01, img.shape).astype(np.float32)
    return np.clip(img, 0, None)


def render_clip(path: Path, seconds: float = 3.0, fps: int = 24, ev: float = 0.0,
                gains=(1.0, 1.0, 1.0), contrast: float = 1.0, saturation: float = 1.0,
                offset: int = 0, ev_ramp: float = 0.0, tag: bool = True) -> Path:
    n = int(seconds * fps)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb48le", "-s", f"{W}x{H}",
           "-r", str(fps), "-i", "-",
           "-vf", "scale=out_color_matrix=bt709:out_range=tv:flags=accurate_rnd,format=yuv420p",
           "-c:v", "libx264", "-crf", "12", "-preset", "veryfast"]
    if tag:
        cmd += ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
    cmd.append(str(path))
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    g = np.asarray(gains, np.float32)
    for i in range(n):
        t = i / fps
        lin = _scene(t, offset) * (2.0 ** (ev + ev_ramp * (i / max(n - 1, 1)))) * g
        sig = colour.oetf_709(lin)
        sig = np.clip(sig, 0, 1)
        if contrast != 1.0:
            sig = np.clip(colour.MID_GREY_SIGNAL + (sig - colour.MID_GREY_SIGNAL) * contrast, 0, 1)
        if saturation != 1.0:
            y = colour.luma(sig)[..., None]
            sig = np.clip(y + (sig - y) * saturation, 0, 1)
        proc.stdin.write((sig * 65535 + 0.5).astype("<u2").tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed to render fixture")
    return path


def make_set(folder: Path) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    return {
        "a_reference": render_clip(folder / "a_reference.mp4", offset=0),
        "b_dark_green": render_clip(folder / "b_dark_green.mp4", ev=-0.7, gains=(0.93, 1.06, 0.95), offset=20),
        "c_warm_flat": render_clip(folder / "c_warm_flat.mp4", ev=0.3, gains=(1.08, 1.0, 0.90), contrast=0.85, offset=40),
        "d_ramp": render_clip(folder / "d_ramp.mp4", ev=0.0, ev_ramp=0.8, offset=10),
    }


if __name__ == "__main__":
    import sys
    print(make_set(Path(sys.argv[1] if len(sys.argv) > 1 else "footage")))
