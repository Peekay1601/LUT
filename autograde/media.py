"""FFmpeg / ffprobe access with explicit colour handling.

Frames never travel through 8-bit PNGs on their way into analysis: FFmpeg
converts YUV -> RGB with an explicit matrix and range and pipes 16-bit RGB to
NumPy. PNG/JPEG files are written only for humans (and for Claude) to look at.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from . import colour

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mxf", ".m4v", ".mkv", ".avi", ".webm", ".mts", ".m2ts"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}


class MediaError(RuntimeError):
    pass


def require_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise MediaError(f"{tool} was not found on PATH")


@dataclass
class ColourInterpretation:
    """How a source will be decoded, and whether that was declared or assumed."""

    primaries: str | None
    transfer: str | None
    matrix: str | None
    range: str | None  # "tv" (limited) or "pc" (full)
    decode_matrix: str = "bt709"
    decode_range: str = "tv"
    supported: bool = True
    assumed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass
class MediaInfo:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    pix_fmt: str
    codec: str
    bit_depth: int
    interpretation: ColourInterpretation

    def to_dict(self) -> dict:
        return asdict(self)


def _fraction(text: str | None) -> float:
    if not text or text in ("0/0", "N/A"):
        return 0.0
    if "/" in text:
        num, den = text.split("/", 1)
        return float(num) / float(den) if float(den) else 0.0
    return float(text)


def interpret_colour(stream: dict, assume_rec709: bool) -> ColourInterpretation:
    """Decide how to decode a stream; reject anything we can't handle honestly."""
    prim = stream.get("color_primaries")
    trc = stream.get("color_transfer")
    mat = stream.get("color_space")
    rng = stream.get("color_range")
    ci = ColourInterpretation(primaries=prim, transfer=trc, matrix=mat, range=rng)

    def check(value, accepted, label):
        if value in (None, "unknown", "unspecified", "reserved"):
            if assume_rec709:
                ci.assumed.append(f"{label} not declared; assumed Rec.709")
            else:
                ci.supported = False
                ci.problems.append(
                    f"{label} not declared (rerun with --assume-rec709 if you know it is Rec.709)"
                )
        elif value not in accepted:
            ci.supported = False
            ci.problems.append(f"unsupported {label} '{value}' (MVP handles SDR Rec.709 only)")

    check(prim, colour.ACCEPTED_PRIMARIES, "primaries")
    check(trc, colour.ACCEPTED_TRANSFERS, "transfer")
    # Rec.601 matrices show up on SD material; we only decode with bt709.
    check(mat, colour.ACCEPTED_MATRICES | {"smpte170m", "bt470bg"}, "matrix")
    if mat in ("smpte170m", "bt470bg"):
        ci.decode_matrix = "bt601"

    if rng in ("pc", "full", "jpeg"):
        ci.decode_range = "pc"
    elif rng in ("tv", "limited", "mpeg"):
        ci.decode_range = "tv"
    else:
        ci.decode_range = "tv"
        ci.assumed.append("range not declared; assumed limited (16-235)")
    return ci


def probe(path: Path, assume_rec709: bool = False) -> MediaInfo:
    require_tools()
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "stream=codec_name,width,height,pix_fmt,r_frame_rate,avg_frame_rate,"
        "color_range,color_space,color_transfer,color_primaries,bits_per_raw_sample,duration,nb_frames"
        ":format=duration",
        "-of", "json", str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise MediaError(f"ffprobe failed on {path}: {res.stderr.strip()}")
    data = json.loads(res.stdout)
    streams = data.get("streams") or []
    if not streams:
        raise MediaError(f"no video stream in {path}")
    s = streams[0]
    duration = float(s.get("duration") or data.get("format", {}).get("duration") or 0.0)
    fps = _fraction(s.get("avg_frame_rate")) or _fraction(s.get("r_frame_rate"))
    pix_fmt = s.get("pix_fmt", "")
    m = re.search(r"p(\d+)", pix_fmt)
    bit_depth = int(s.get("bits_per_raw_sample") or (m.group(1) if m else 8))
    interp = interpret_colour(s, assume_rec709)
    return MediaInfo(
        path=str(path), duration=duration, width=int(s["width"]), height=int(s["height"]),
        fps=fps, pix_fmt=pix_fmt, codec=s.get("codec_name", ""), bit_depth=bit_depth,
        interpretation=interp,
    )


def _cache_path(cache_dir: Path, info: MediaInfo, t: float, vf: str) -> Path:
    st = os.stat(info.path)
    key = f"{os.path.abspath(info.path)}|{st.st_size}|{st.st_mtime_ns}|{t:.4f}|{vf}"
    return cache_dir / (hashlib.sha1(key.encode()).hexdigest() + ".npy")


def extract_frame(info: MediaInfo, t: float, width: int = 960, cache_dir: Path | None = None) -> np.ndarray:
    """Decode one frame at ``t`` seconds to a float32 Rec.709 signal array in [0, 1].

    With ``cache_dir`` the lossless 16-bit decode is cached, keyed on the file's
    path, size and mtime plus the exact decode settings, so a changed file is
    never served from a stale cache.
    """
    ci = info.interpretation
    out_w = min(width, info.width)
    out_h = int(round(info.height * out_w / info.width / 2)) * 2
    vf = (
        f"scale={out_w}:{out_h}:in_color_matrix={ci.decode_matrix}:in_range={ci.decode_range}"
        f":out_range=pc:flags=accurate_rnd+full_chroma_int+bicubic,format=rgb48le"
    )
    cached = _cache_path(cache_dir, info, t, vf) if cache_dir else None
    if cached is not None and cached.exists():
        return np.load(cached).astype(np.float32) / 65535.0
    cmd = [
        "ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(t, 0):.4f}", "-i", info.path,
        "-frames:v", "1", "-vf", vf, "-f", "rawvideo", "-",
    ]
    res = subprocess.run(cmd, capture_output=True)
    expected = out_w * out_h * 3 * 2
    if res.returncode != 0 or len(res.stdout) < expected:
        raise MediaError(f"could not decode frame at {t:.3f}s from {info.path}: {res.stderr.decode().strip()}")
    arr = np.frombuffer(res.stdout[:expected], dtype="<u2").reshape(out_h, out_w, 3)
    if cached is not None:
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(".tmp.npy")
        np.save(tmp, arr)
        os.replace(tmp, cached)
    return (arr.astype(np.float32) / 65535.0)


def detect_scenes(info: MediaInfo, threshold: float = 0.3, min_shot: float = 0.5) -> list[tuple[float, float]]:
    """Split a file into shots using FFmpeg's scene score. Returns (start, end) seconds."""
    cmd = [
        "ffmpeg", "-v", "info", "-nostdin", "-i", info.path, "-an",
        "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    cuts = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", res.stderr)]
    bounds = [0.0] + [c for c in cuts if 0 < c < info.duration] + [info.duration]
    shots: list[tuple[float, float]] = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a < min_shot and shots:
            shots[-1] = (shots[-1][0], b)  # absorb tiny fragments (flashes, dissolve tails)
        elif b - a >= 1e-3:
            shots.append((a, b))
    return shots


def _srgb_to_linear(x: np.ndarray) -> np.ndarray:
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4).astype(np.float32)


def load_image_as_signal(path: Path, width: int = 960) -> np.ndarray:
    """Load a still reference (assumed sRGB, as almost all stills are) as Rec.709 signal.

    sRGB and Rec.709 share primaries but not transfer functions, so the image is
    linearised with the sRGB EOTF and re-encoded with the Rec.709 OETF instead of
    being treated as if the two encodings were identical.
    """
    img = Image.open(path)
    if img.mode not in ("RGB", "I;16", "RGB;16"):
        img = img.convert("RGB")
    if img.width > width:
        img = img.resize((width, int(round(img.height * width / img.width))), Image.Resampling.BICUBIC)
    arr = np.asarray(img).astype(np.float32)
    arr /= 65535.0 if arr.max() > 255 else 255.0
    return colour.oetf_709(_srgb_to_linear(arr))


def signal_to_display_image(sig: np.ndarray) -> Image.Image:
    """Make a viewable 8-bit image from a Rec.709 signal array.

    Rec.709 signal is shown as-is (the usual "video on a computer monitor"
    convention, matching what Premiere shows with display colour management off).
    """
    return Image.fromarray((np.clip(sig, 0, 1) * 255.0 + 0.5).astype(np.uint8), "RGB")


def find_inputs(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS)
