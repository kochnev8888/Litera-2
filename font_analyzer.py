"""Геометрический анализ TTF/OTF для обратного проектирования параметров.

Модуль измеряет эталонные глифы (H/O/n/o и кириллические аналоги),
определяет насыщенность, ширину, наклон, контраст и наличие засечек.
Результат приводится к сетке 1000 units-per-em, чтобы его можно было
передать в ``font_engine.generate_font`` без дополнительных пересчётов.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.ttLib import TTFont

Point = tuple[float, float]
Interval = tuple[float, float]

# Эмпирическая шкала соответствия толщины ствола и класса насыщенности.
_WEIGHT_POINTS: tuple[tuple[float, int], ...] = (
    (0.020, 100),
    (0.040, 200),
    (0.060, 300),
    (0.085, 400),
    (0.100, 500),
    (0.115, 600),
    (0.135, 700),
    (0.160, 800),
    (0.190, 900),
)


def _sample_quad(p0: Point, p1: Point, p2: Point, steps: int = 12) -> list[Point]:
    points: list[Point] = []
    for index in range(1, steps + 1):
        t = index / steps
        mt = 1.0 - t
        points.append(
            (
                mt * mt * p0[0] + 2.0 * mt * t * p1[0] + t * t * p2[0],
                mt * mt * p0[1] + 2.0 * mt * t * p1[1] + t * t * p2[1],
            )
        )
    return points


def _sample_cubic(p0: Point, p1: Point, p2: Point, p3: Point, steps: int = 12) -> list[Point]:
    points: list[Point] = []
    for index in range(1, steps + 1):
        t = index / steps
        mt = 1.0 - t
        points.append(
            (
                mt**3 * p0[0] + 3.0 * mt * mt * t * p1[0] + 3.0 * mt * t * t * p2[0] + t**3 * p3[0],
                mt**3 * p0[1] + 3.0 * mt * mt * t * p1[1] + 3.0 * mt * t * t * p2[1] + t**3 * p3[1],
            )
        )
    return points


def glyph_contours(glyph_set, glyph_name: str) -> list[list[Point]]:
    """Возвращает замкнутые полигоны глифа, разложив кривые в отрезки."""

    pen = DecomposingRecordingPen(glyph_set)
    glyph_set[glyph_name].draw(pen)
    contours: list[list[Point]] = []
    current: list[Point] = []
    start: Point | None = None

    for operator, args in pen.value:
        if operator == "moveTo":
            if current:
                contours.append(current)
            start = args[0]
            current = [start]
        elif operator == "lineTo":
            current.append(args[0])
        elif operator == "qCurveTo":
            points = list(args)
            if not current:
                # Некоторые генераторы опускают moveTo у первого контура.
                seed = next((point for point in reversed(points) if point is not None), (0.0, 0.0))
                start = seed
                current = [seed]
            end = start if points[-1] is None else points[-1]
            controls = points[:-1]
            previous = current[-1]
            if not controls:
                current.append(end)
            elif len(controls) == 1:
                current.extend(_sample_quad(previous, controls[0], end))
            else:
                anchor = previous
                for index in range(len(controls) - 1):
                    midpoint = (
                        (controls[index][0] + controls[index + 1][0]) / 2.0,
                        (controls[index][1] + controls[index + 1][1]) / 2.0,
                    )
                    current.extend(_sample_quad(anchor, controls[index], midpoint))
                    anchor = midpoint
                current.extend(_sample_quad(anchor, controls[-1], end))
        elif operator == "curveTo":
            points = list(args)
            previous = current[-1]
            for index in range(0, len(points), 3):
                first, second, end = points[index : index + 3]
                current.extend(_sample_cubic(previous, first, second, end))
                previous = end
        elif operator in ("closePath", "endPath"):
            if current:
                if current[-1] != current[0]:
                    current.append(current[0])
                contours.append(current)
                current = []
    if current:
        if current[-1] != current[0]:
            current.append(current[0])
        contours.append(current)
    return contours


def _bounds(contours: Sequence[Sequence[Point]]) -> tuple[float, float, float, float]:
    xs = [point[0] for contour in contours for point in contour]
    ys = [point[1] for contour in contours for point in contour]
    if not xs or not ys:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


def horizontal_intervals(contours: Sequence[Sequence[Point]], y: float) -> list[Interval]:
    """Интервалы заливки вдоль горизонтальной линии ``y``."""

    crossings: list[float] = []
    for contour in contours:
        for first, second in zip(contour, contour[1:]):
            if (first[1] <= y < second[1]) or (second[1] <= y < first[1]):
                crossings.append(
                    first[0] + (y - first[1]) * (second[0] - first[0]) / (second[1] - first[1])
                )
    crossings.sort()
    return [(crossings[index], crossings[index + 1]) for index in range(0, len(crossings) - 1, 2)]


def vertical_intervals(contours: Sequence[Sequence[Point]], x: float) -> list[Interval]:
    """Интервалы заливки вдоль вертикальной линии ``x``."""

    crossings: list[float] = []
    for contour in contours:
        for first, second in zip(contour, contour[1:]):
            if (first[0] <= x < second[0]) or (second[0] <= x < first[0]):
                crossings.append(
                    first[1] + (x - first[0]) * (second[1] - first[1]) / (second[0] - first[0])
                )
    crossings.sort()
    return [(crossings[index], crossings[index + 1]) for index in range(0, len(crossings) - 1, 2)]


def _average_width(intervals: Iterable[Interval], maximum: float | None = None) -> float:
    widths = [right - left for left, right in intervals if right > left]
    if maximum is not None:
        widths = [width for width in widths if width <= maximum]
    return sum(widths) / len(widths) if widths else 0.0


def _glyph_for(font: TTFont, *characters: str) -> str | None:
    cmap = font.getBestCmap() or {}
    for character in characters:
        name = cmap.get(ord(character))
        if name:
            return name
    return None


def _stem_width(contours: Sequence[Sequence[Point]], cap_height: float, upem: float) -> float:
    """Толщина вертикального ствола H на нескольких уровнях выше перекладины."""

    candidates: list[float] = []
    for fraction in (0.62, 0.68, 0.74, 0.80):
        intervals = horizontal_intervals(contours, cap_height * fraction)
        width = _average_width(intervals, maximum=0.35 * upem)
        if width:
            candidates.append(width)
    return sorted(candidates)[len(candidates) // 2] if candidates else 0.0


def _measure_contrast(
    font: TTFont, glyph_set, o_name: str | None, h_contours: Sequence[Sequence[Point]], cap_height: float, upem: float
) -> float:
    """Отношение горизонтальных штрихов к вертикальным по глифу O."""

    if o_name:
        contours = glyph_contours(glyph_set, o_name)
        x_min, y_min, x_max, y_max = _bounds(contours)
        if x_max > x_min and y_max > y_min:
            center_x = (x_min + x_max) / 2.0
            center_y = (y_min + y_max) / 2.0
            horizontal = _average_width(vertical_intervals(contours, center_x), maximum=0.3 * upem)
            vertical = _average_width(horizontal_intervals(contours, center_y), maximum=0.3 * upem)
            if horizontal and vertical:
                return max(0.0, min(1.0, 1.0 - horizontal / vertical))
    # Запасной вариант: перекладина H.
    if h_contours:
        x_min, _, x_max, _ = _bounds(h_contours)
        center_x = (x_min + x_max) / 2.0
        intervals = vertical_intervals(h_contours, center_x)
        if len(intervals) >= 3:
            middle = sorted(intervals, key=lambda item: item[1] - item[0])[len(intervals) // 2]
            horizontal = middle[1] - middle[0]
            vertical = _stem_width(h_contours, cap_height, upem)
            if vertical:
                return max(0.0, min(1.0, 1.0 - horizontal / vertical))
    return 0.0


def _measure_slant(contours: Sequence[Sequence[Point]], cap_height: float) -> float:
    if not contours:
        return 0.0
    lower = horizontal_intervals(contours, cap_height * 0.25)
    upper = horizontal_intervals(contours, cap_height * 0.75)
    if not lower or not upper:
        return 0.0
    lower_center = (lower[0][0] + lower[0][1]) / 2.0
    upper_center = (upper[0][0] + upper[0][1]) / 2.0
    angle = math.degrees(math.atan2(upper_center - lower_center, cap_height * 0.5))
    return max(-15.0, min(15.0, angle))


def _has_serifs(contours: Sequence[Sequence[Point]], cap_height: float, upem: float) -> bool:
    if not contours:
        return False
    baseline = _average_width(horizontal_intervals(contours, cap_height * 0.03), maximum=0.4 * upem)
    middle = _average_width(horizontal_intervals(contours, cap_height * 0.20), maximum=0.4 * upem)
    if not baseline or not middle:
        return False
    return baseline > middle * 1.25


def stem_to_weight(ratio: float) -> int:
    """Переводит отношение толщины ствола к em в класс насыщенности."""

    if ratio <= _WEIGHT_POINTS[0][0]:
        return _WEIGHT_POINTS[0][1]
    if ratio >= _WEIGHT_POINTS[-1][0]:
        return _WEIGHT_POINTS[-1][1]
    for (left_ratio, left_weight), (right_ratio, right_weight) in zip(_WEIGHT_POINTS, _WEIGHT_POINTS[1:]):
        if left_ratio <= ratio <= right_ratio:
            span = right_ratio - left_ratio
            position = (ratio - left_ratio) / span if span else 0.0
            value = left_weight + position * (right_weight - left_weight)
            return int(round(value / 10.0) * 10)
    return 400


def _normalized_metrics(font: TTFont) -> tuple[int, int, int, int]:
    upem = font["head"].unitsPerEm or 1000
    scale = 1000.0 / upem
    os2 = font.get("OS/2")
    hhea = font["hhea"]
    ascender = getattr(os2, "sTypoAscender", None) or hhea.ascent or 800
    descender = getattr(os2, "sTypoDescender", None) or hhea.descent or -200
    ascender = max(600, min(1000, int(round(ascender * scale))))
    descender = max(-500, min(0, int(round(descender * scale))))
    return upem, ascender, descender, int(round(1000))


def analyze_font(font: TTFont, base_font_id: str | None = None) -> dict:
    """Извлекает параметры гарнитуры из контуров и возвращает JSON-профиль."""

    upem, ascender, descender, _ = _normalized_metrics(font)
    scale = 1000.0 / upem
    cmap = font.getBestCmap() or {}
    glyph_set = font.getGlyphSet()

    h_name = _glyph_for(font, "H", "Н")
    o_name = _glyph_for(font, "O", "О")
    x_name = _glyph_for(font, "x", "х")
    notes: list[str] = []

    h_contours = glyph_contours(glyph_set, h_name) if h_name else []
    o_contours = glyph_contours(glyph_set, o_name) if o_name else []
    x_contours = glyph_contours(glyph_set, x_name) if x_name else []

    os2 = font.get("OS/2")
    declared_cap = getattr(os2, "sCapHeight", 0) if os2 else 0
    declared_x = getattr(os2, "sxHeight", 0) if os2 else 0

    h_bounds = _bounds(h_contours)
    cap_height = h_bounds[3] if h_bounds[3] > 0 else 0.0
    if cap_height <= 0 and o_contours:
        cap_height = _bounds(o_contours)[3]
    if 300 <= declared_cap <= 1000:
        cap_height = declared_cap
    if cap_height <= 0:
        cap_height = declared_cap or upem * 0.7
        notes.append("Глиф H не найден: высота прописных взята из OS/2.")
    x_height = _bounds(x_contours)[3] if x_contours else 0.0
    if x_height <= 0 and o_contours:
        x_height = _bounds(o_contours)[3]
    if 200 <= declared_x <= 900:
        x_height = declared_x
    if x_height <= 0:
        x_height = declared_x or upem * 0.5
        notes.append("Глиф x не найден: высота строчных взята из OS/2.")

    stem = _stem_width(h_contours, cap_height, upem) if h_contours else 0.0
    if not stem:
        stem = upem * 0.085
        notes.append("Толщина ствола не измерена: использовано типовое значение.")

    declared_weight = getattr(os2, "usWeightClass", None) if os2 else None
    if declared_weight and 100 <= declared_weight <= 900:
        weight = int(round(declared_weight / 10.0) * 10)
    else:
        weight = stem_to_weight(stem / upem)
    contrast = _measure_contrast(font, glyph_set, o_name, h_contours, cap_height, upem)
    slant = _measure_slant(h_contours, cap_height)
    if "post" in font:
        declared_slant = -float(getattr(font["post"], "italicAngle", 0.0) or 0.0)
        if abs(declared_slant) >= 1:
            slant = declared_slant
    serif = _has_serifs(h_contours, cap_height, upem)

    width_map = {1: 50, 2: 62, 3: 75, 4: 100, 5: 112, 6: 125, 7: 150, 8: 175, 9: 200}
    declared_width = getattr(os2, "usWidthClass", None) if os2 else None
    if declared_width in width_map:
        width = width_map[declared_width]
    elif o_contours:
        o_bounds = _bounds(o_contours)
        o_width = o_bounds[2] - o_bounds[0]
        o_height = o_bounds[3] - o_bounds[1]
        width = int(round(o_width / o_height * 100.0)) if o_height else 100
    else:
        advance = font["hmtx"].metrics.get(h_name, (upem, 0))[0] if h_name else upem
        width = int(round(advance / cap_height * 100.0)) if cap_height else 100
        notes.append("Глиф O не найден: ширина оценена по метрикам H.")
    width = max(50, min(200, width))

    cap_units = max(500, min(900, int(round(cap_height * scale))))
    x_units = max(300, min(800, int(round(x_height * scale))))
    if x_units >= cap_units:
        x_units = max(300, cap_units - 60)
    if cap_units > ascender:
        ascender = min(1000, cap_units)

    cyrillic_positions = list(range(0x0400, 0x0500))
    cyrillic_total = sum(1 for code in cyrillic_positions if code in cmap)
    cyrillic_required = sum(1 for code in range(0x0410, 0x0450) if code in cmap) + sum(
        1 for code in (0x0401, 0x0451) if code in cmap
    )
    if cyrillic_required < 64:
        notes.append("В шрифте неполный набор кириллицы: часть символов может отсутствовать.")

    parameters = {
        "weight": weight,
        "width": width,
        "slant": int(round(slant)),
        "contrast": round(contrast, 3),
        "roundness": 0.0,
        "sharpness": 0.5,
        "serif": bool(serif),
        "terminal_style": "flat",
        "stroke_contrast_axis": "vertical",
        "x_height": x_units,
        "cap_height": cap_units,
        "overshoot": True,
        "ink_trap": False,
        "mood": "neutral",
        "style": "editorial" if serif else "modern",
        "purpose": "print",
        "language_support": ["latin", "cyrillic"] if cyrillic_required else ["latin"],
    }
    metrics = {"units_per_em": 1000, "ascender": ascender, "descender": descender}

    family = font["name"].getDebugName(16) or font["name"].getDebugName(1) or "Без названия"
    style = font["name"].getDebugName(17) or font["name"].getDebugName(2) or "Regular"
    return {
        "base_font_id": base_font_id or "",
        "family": family,
        "style": style,
        "format": "TTF" if "glyf" in font else "OTF",
        "glyph_count": len(font.getGlyphOrder()),
        "unicode_count": len(cmap),
        "cyrillic_coverage": {
            "block": cyrillic_total,
            "required": cyrillic_required,
            "total_required": 66,
        },
        "parameters": parameters,
        "metrics": metrics,
        "notes": notes,
    }
