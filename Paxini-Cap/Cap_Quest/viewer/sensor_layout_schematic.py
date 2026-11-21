"""Schematic fallback for the distribution-force point layout.

Used when the local ``sensor_layout.py`` (PXCap Studio point model) is absent.
The grids are evenly spaced and contain no vendor point coordinates.  Their
extents mirror the real sensor models' aspect ratios, so the 119-point palm
pad keeps the same on-screen length as the real model; the points themselves
are still a schematic arrangement, not a calibrated sensor map.
"""

from __future__ import annotations

import math

# Inset every pad inside its reference box so neighbouring slots keep a
# visible gap.  0.10 means 10% of the box on each side (20% total).
PATCH_INSET = 0.10

# Real-hardware orientation check.  These values are our own calibration,
# shared with the local Studio model:
#   "center" = rotate the pattern 180° about the pad centre
#   "v"      = mirror across the pad's vertical centre line
#   "h"      = mirror across the pad's horizontal centre line
SLOT_FLIPS: list[str | None] = [
    "v",  # thumb_one
    "v",  # thumb_two
    "h",  # index_zero
    "h",  # index_one
    "h",  # middle_zero
    "h",  # middle_one
    "h",  # middle_two
    "h",  # middle_three
    "h",  # palm_zero
    "v",  # palm_one
]

# (model, cx, cy, width, height, rotation_deg); cx/cy/width/height are
# normalised to the 0..1 viewer canvas.  Slot order is CAP_SLOTS:
# thumb_one, thumb_two, index_zero, index_one, middle_zero, middle_one,
# middle_two, middle_three, palm_zero, palm_one.
SLOT_LAYOUT: list[tuple[str, float, float, float, float, float]] = [
    ("PAD49", 0.7541, 0.7237, 0.2159, 0.2153, 0),    # thumb_one
    ("PAD20", 0.7549, 0.8900, 0.1664, 0.1077, 90),   # thumb_two
    ("PAD49", 0.7391, 0.2494, 0.2129, 0.2069, 0),    # index_zero
    ("PAD20", 0.7384, 0.0903, 0.1604, 0.1017, 90),   # index_one
    ("PAD49", 0.4543, 0.2494, 0.2129, 0.2069, 0),    # middle_zero
    ("PAD20", 0.4543, 0.0903, 0.1619, 0.1017, 90),   # middle_one
    ("PAD49", 0.2331, 0.2494, 0.2114, 0.2069, 0),    # middle_two
    ("PAD20", 0.2346, 0.0903, 0.1604, 0.1017, 90),   # middle_three
    ("PAD119", 0.4910, 0.4850, 0.7931, 0.2404, 90),  # palm_zero
    ("PAD49", 0.2571, 0.7225, 0.2654, 0.1722, 90),   # palm_one
]


# The point index order mirrors the real Studio models: 49/119 are mostly
# column-major, 20 is a staggered 4x5 pattern.  These are integer (row, col)
# mappings, not vendor coordinates; the grid points themselves stay regular.
PAD49_ORDER: list[tuple[int, int]] = [
    (0, 0), (1, 0), (2, 0), (3, 0), (4, 0), (5, 0), (6, 0),
    (0, 1), (1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (6, 1),
    (0, 2), (1, 2), (2, 2), (3, 2), (4, 2), (5, 2), (6, 2),
    (0, 3), (1, 3), (2, 3), (3, 3), (4, 3), (5, 3), (6, 3),
    (0, 4), (1, 4), (2, 4), (3, 4), (4, 4), (5, 4), (6, 4),
    (0, 5), (1, 5), (2, 5), (3, 5), (4, 5), (5, 5), (6, 5),
    (0, 6), (1, 6), (2, 6), (3, 6), (4, 6), (5, 6), (6, 6),
]
PAD20_ORDER: list[tuple[int, int]] = [
    (4, 0), (4, 1), (4, 2), (4, 3), (3, 0), (3, 3), (3, 1),
    (2, 1), (3, 2), (2, 3), (1, 0), (1, 1), (1, 2), (1, 3),
    (0, 0), (0, 3), (0, 1), (0, 2), (2, 2), (2, 0),
]
PAD119_ORDER: list[tuple[int, int]] = [
    (4, 0), (3, 0), (2, 0), (1, 0), (0, 0), (0, 1), (1, 1),
    (2, 1), (3, 1), (4, 1), (8, 1), (7, 1), (6, 1), (5, 1),
    (4, 2), (5, 2), (6, 2), (7, 2), (8, 2), (8, 0), (7, 0),
    (6, 0), (5, 0), (0, 2), (1, 2), (2, 2), (3, 2), (8, 3),
    (4, 3), (5, 3), (6, 3), (7, 3), (0, 3), (1, 3), (2, 3),
    (12, 3), (3, 3), (12, 0), (13, 0), (14, 0), (11, 3), (10, 3),
    (15, 0), (16, 0), (16, 1), (15, 1), (14, 1), (13, 1), (9, 3),
    (12, 1), (9, 1), (10, 1), (11, 1), (12, 2), (16, 3), (15, 3),
    (11, 2), (10, 2), (9, 2), (14, 3), (13, 3), (9, 0), (10, 0),
    (11, 0), (16, 2), (15, 2), (14, 2), (13, 2), (12, 6), (13, 6),
    (14, 6), (15, 6), (16, 6), (16, 5), (15, 5), (14, 5), (13, 5),
    (12, 5), (8, 5), (9, 5), (10, 5), (11, 5), (12, 4), (11, 4),
    (10, 4), (9, 4), (8, 4), (8, 6), (9, 6), (10, 6), (11, 6),
    (16, 4), (15, 4), (14, 4), (13, 4), (4, 6), (3, 6), (2, 6),
    (1, 6), (0, 6), (0, 5), (1, 5), (2, 5), (3, 5), (4, 5),
    (7, 5), (6, 5), (5, 5), (4, 4), (5, 4), (6, 4), (7, 4),
    (7, 6), (6, 6), (5, 6), (0, 4), (1, 4), (2, 4), (3, 4),
]


def _grid(
    cols: int,
    rows: int,
    span_x: float,
    span_y: float,
    order: list[tuple[int, int]],
) -> list[list[float]]:
    """Place each index at its (row, col) cell, including both edges."""
    xs = (
        [0.0]
        if cols == 1
        else [-span_x / 2.0 + i * span_x / (cols - 1) for i in range(cols)]
    )
    ys = (
        [0.0]
        if rows == 1
        else [-span_y / 2.0 + i * span_y / (rows - 1) for i in range(rows)]
    )
    return [[xs[c], ys[r]] for r, c in order]


# Spans measured from the real Studio point models (normalised by the longer
# axis).  Keeping the same aspect ratio preserves the 119-point pad length.
MODELS: dict[str, list[list[float]]] = {
    "PAD49": _grid(7, 7, 0.8058, 1.0, PAD49_ORDER),
    "PAD20": _grid(4, 5, 0.7878, 1.0, PAD20_ORDER),
    "PAD119": _grid(7, 17, 0.3738, 1.0, PAD119_ORDER),
}


def build_dist_rotations() -> list[float]:
    """Per-slot pad rotation in degrees (same order as CAP_SLOTS)."""
    return [rotation for *_, rotation in SLOT_LAYOUT]


def build_dist_flips() -> list[str | None]:
    """Per-slot pattern flips: "center" (180°), "v", "h", or None."""
    return list(SLOT_FLIPS)


def build_dist_points() -> list[list[list[float]]]:
    """Return 10 slots of 2D points normalised to the 0..1 canvas."""
    slots: list[list[list[float]]] = []
    for slot_index, (model, cx, cy, width, height, rotation) in enumerate(
        SLOT_LAYOUT
    ):
        source = MODELS[model]
        xs = [p[0] for p in source]
        ys = [p[1] for p in source]
        span_x = max(xs) - min(xs)
        span_y = max(ys) - min(ys)
        angle = math.radians(rotation)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        # Fit the rotated bounding box so a 90° pad is not scaled by its
        # unrotated span.
        rotated_w = abs(cos_a) * span_x + abs(sin_a) * span_y
        rotated_h = abs(sin_a) * span_x + abs(cos_a) * span_y
        usable_w = width * (1.0 - 2.0 * PATCH_INSET)
        usable_h = height * (1.0 - 2.0 * PATCH_INSET)
        scale = min(usable_w / rotated_w, usable_h / rotated_h)
        flip = SLOT_FLIPS[slot_index]
        points: list[list[float]] = []
        for nx, ny in source:
            px, py = nx * scale, ny * scale
            rx = px * cos_a - py * sin_a
            ry = px * sin_a + py * cos_a
            fx = cx + rx
            fy = cy + ry
            if flip == "center":
                fx = 2.0 * cx - fx
                fy = 2.0 * cy - fy
            elif flip == "v":
                fx = 2.0 * cx - fx
            elif flip == "h":
                fy = 2.0 * cy - fy
            points.append([round(fx, 4), round(fy, 4)])
        slots.append(points)
    return slots


__all__ = [
    "build_dist_points",
    "build_dist_rotations",
    "build_dist_flips",
]
