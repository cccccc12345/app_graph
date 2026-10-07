"""Screenshot perceptual hash + SSIM state matcher (Pillow required)."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # Keep pure hash/SSIM helpers testable without optional runtime setup.
    Image = None


@dataclass(frozen=True)
class MatchScore:
    phash_distance: int
    ssim: float


def _prepared(path: str | Path):
    if Image is None:
        raise RuntimeError("Pillow is required. Install with: python -m pip install -e .")
    image = Image.open(path).convert("L")
    width, height = image.size
    # Trim status/navigation bars, whose clock, battery, and gesture hint are
    # not useful for distinguishing app pages.
    top = int(height * 0.055)
    bottom = int(height * 0.94)
    image = image.crop((0, top, width, max(top + 1, bottom)))
    # Keep aspect ratio for SSIM; pHash is intentionally normalized to square.
    ssim_width = 96
    ssim_height = max(32, round(image.height * ssim_width / image.width))
    ssim_image = image.resize((ssim_width, ssim_height), Image.Resampling.LANCZOS)
    hash_image = image.resize((32, 32), Image.Resampling.LANCZOS)
    return ssim_image, hash_image


def _phash(image) -> int:
    """Compute a 64-bit DCT perceptual hash using only Pillow + stdlib."""
    pixels = list(image.get_flattened_data())
    n = 32
    # Compute horizontal cosine projections once for each row/frequency. This
    # separable 2-D DCT avoids repeating the inner pixel loops 64 times.
    basis = [[math.cos((2 * p + 1) * f * math.pi / (2 * n)) for p in range(n)] for f in range(8)]
    horizontal: list[list[float]] = []
    for y in range(n):
        row_offset = y * n
        row = []
        for u in range(8):
            row.append(sum(pixels[row_offset + x] * basis[u][x] for x in range(n)))
        horizontal.append(row)

    values: list[float] = []
    for v in range(8):
        cv = 1 / math.sqrt(2) if v == 0 else 1.0
        for u in range(8):
            cu = 1 / math.sqrt(2) if u == 0 else 1.0
            coeff = sum(horizontal[y][u] * basis[v][y] for y in range(n)) * cu * cv
            values.append(coeff)
    median = statistics.median(values[1:])
    result = 0
    for value in values:
        result = (result << 1) | int(value > median)
    return result


def _box_sum(integral: list[list[float]], x0: int, y0: int, x1: int, y1: int) -> float:
    return integral[y1][x1] - integral[y0][x1] - integral[y1][x0] + integral[y0][x0]


def _integral(values: list[list[float]]) -> list[list[float]]:
    height, width = len(values), len(values[0])
    result = [[0.0] * (width + 1) for _ in range(height + 1)]
    for y, row in enumerate(values, start=1):
        running = 0.0
        previous = result[y - 1]
        current = result[y]
        for x, value in enumerate(row, start=1):
            running += value
            current[x] = previous[x] + running
    return result


def _ssim(a, b) -> float:
    """Mean local SSIM using 7x7 uniform windows and integral images."""
    if a.size != b.size:
        b = b.resize(a.size, Image.Resampling.LANCZOS)
    width, height = a.size
    av = list(a.get_flattened_data())
    bv = list(b.get_flattened_data())
    rows_a = [list(map(float, av[y * width : (y + 1) * width])) for y in range(height)]
    rows_b = [list(map(float, bv[y * width : (y + 1) * width])) for y in range(height)]
    sum_a = _integral(rows_a)
    sum_b = _integral(rows_b)
    sum_aa = _integral([[v * v for v in row] for row in rows_a])
    sum_bb = _integral([[v * v for v in row] for row in rows_b])
    sum_ab = _integral([[x * y for x, y in zip(ra, rb)] for ra, rb in zip(rows_a, rows_b)])
    radius = 3
    n = 49.0
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    total = 0.0
    count = 0
    # Non-overlapping sample stride keeps matching fast while averaging local
    # luminance/contrast/structure rather than a single global statistic.
    for y in range(radius, height - radius, 4):
        y0, y1 = y - radius, y + radius + 1
        for x in range(radius, width - radius, 4):
            x0, x1 = x - radius, x + radius + 1
            ma = _box_sum(sum_a, x0, y0, x1, y1) / n
            mb = _box_sum(sum_b, x0, y0, x1, y1) / n
            va = max(0.0, _box_sum(sum_aa, x0, y0, x1, y1) / n - ma * ma)
            vb = max(0.0, _box_sum(sum_bb, x0, y0, x1, y1) / n - mb * mb)
            cov = _box_sum(sum_ab, x0, y0, x1, y1) / n - ma * mb
            total += ((2 * ma * mb + c1) * (2 * cov + c2)) / ((ma * ma + mb * mb + c1) * (va + vb + c2))
            count += 1
    return total / max(1, count)


def compare_screenshots(first: str | Path, second: str | Path) -> MatchScore:
    a, ah = _prepared(first)
    b, bh = _prepared(second)
    hash_a, hash_b = _phash(ah), _phash(bh)
    return MatchScore((hash_a ^ hash_b).bit_count(), _ssim(a, b))
