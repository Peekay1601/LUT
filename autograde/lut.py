"""3D LUT (.cube) export built from the *same* ``apply_grade`` used for previews."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .grade import Look, ShotGrade, apply_grade


def lattice(size: int) -> np.ndarray:
    """(size**3, 3) grid in .cube order: red changes fastest, then green, then blue."""
    v = np.linspace(0.0, 1.0, size, dtype=np.float32)
    b, g, r = np.meshgrid(v, v, v, indexing="ij")
    return np.stack([r.ravel(), g.ravel(), b.ravel()], axis=-1)


def build_lut(grade: ShotGrade | None, look: Look | None, size: int = 33) -> np.ndarray:
    grid = lattice(size)
    return apply_grade(grid.reshape(1, -1, 3), grade, look).reshape(-1, 3)


def write_cube(path: Path, table: np.ndarray, size: int, title: str, comments: list[str] = ()) -> None:
    lines = [f'TITLE "{title}"']
    lines += [f"# {c}" for c in comments]
    lines += [f"LUT_3D_SIZE {size}", "DOMAIN_MIN 0.0 0.0 0.0", "DOMAIN_MAX 1.0 1.0 1.0"]
    lines += [f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in table]
    path.write_text("\n".join(lines) + "\n")


def read_cube(path: Path) -> tuple[int, np.ndarray]:
    size, rows = 0, []
    for line in path.read_text().splitlines():
        t = line.strip()
        if not t or t.startswith("#") or t.startswith("TITLE") or t.startswith("DOMAIN"):
            continue
        if t.startswith("LUT_3D_SIZE"):
            size = int(t.split()[1])
            continue
        rows.append([float(x) for x in t.split()])
    return size, np.asarray(rows, dtype=np.float32)


def apply_cube(sig: np.ndarray, size: int, table: np.ndarray) -> np.ndarray:
    """Trilinear LUT application, used to verify LUT vs. direct-transform previews."""
    cube = table.reshape(size, size, size, 3)  # [b, g, r]
    x = np.clip(sig, 0, 1) * (size - 1)
    i0 = np.floor(x).astype(int)
    i1 = np.minimum(i0 + 1, size - 1)
    f = x - i0
    r0, g0, b0 = i0[..., 0], i0[..., 1], i0[..., 2]
    r1, g1, b1 = i1[..., 0], i1[..., 1], i1[..., 2]
    fr, fg, fb = f[..., 0:1], f[..., 1:2], f[..., 2:3]

    def c(b, g, r):
        return cube[b, g, r]

    c00 = c(b0, g0, r0) * (1 - fr) + c(b0, g0, r1) * fr
    c01 = c(b0, g1, r0) * (1 - fr) + c(b0, g1, r1) * fr
    c10 = c(b1, g0, r0) * (1 - fr) + c(b1, g0, r1) * fr
    c11 = c(b1, g1, r0) * (1 - fr) + c(b1, g1, r1) * fr
    c0 = c00 * (1 - fg) + c01 * fg
    c1 = c10 * (1 - fg) + c11 * fg
    return (c0 * (1 - fb) + c1 * fb).astype(np.float32)


def lut_error(sig: np.ndarray, grade: ShotGrade | None, look: Look | None, size: int) -> dict:
    """Max / mean difference between the direct transform and its LUT approximation (8-bit code values)."""
    table = build_lut(grade, look, size)
    direct = apply_grade(sig, grade, look)
    via_lut = apply_cube(sig, size, table)
    d = np.abs(direct - via_lut) * 255.0
    return {"size": size, "max_code_values": round(float(d.max()), 3), "mean_code_values": round(float(d.mean()), 4)}
