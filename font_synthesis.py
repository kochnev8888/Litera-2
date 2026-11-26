"""Синтез и препресс-сборка шрифта поверх существующего движка.

Модуль использует те же эталонные гарнитуры и ``read_font`` из
``font_engine``, но добавляет параметрическую обработку контуров:
насыщенность, ширину, наклон, контраст, скругление, высоту строчных и
прописных, а также корректное заполнение системных OpenType-таблиц.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
import unicodedata
import uuid
from functools import lru_cache
from io import BytesIO

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

from font_analyzer import analyze_font
from font_engine import FONT_DIR, PRESETS, RequestError, preset_data, read_font
from font_schema import FontMetrics, FontParameters
from font_storage import load_font

try:  # skia-pathops нужен для булевых операций; без него работает базовый режим.
    import pathops
except Exception:  # pragma: no cover - зависит от окружения
    pathops = None


BASE_FONT_SPECS = {
    "editorial": {
        "family": "Literata / Lora",
        "serif": True,
        "styles": ("editorial", "book", "serif", "classic", "журнал", "книг"),
    },
    "grotesk": {
        "family": "Inter / IBM Plex Sans",
        "serif": False,
        "styles": ("modern", "geometric", "minimal", "neutral", "screen", "интерфейс"),
    },
    "mono": {
        "family": "JetBrains Mono",
        "serif": False,
        "styles": ("techno", "cyberpunk", "mono", "technical", "code", "терминал"),
    },
}

REQUIRED_CODES = (
    set(range(0x0410, 0x0450))
    | {0x0401, 0x0451}
    | set(range(0x0041, 0x005B))
    | set(range(0x0061, 0x007B))
    | set(range(0x0030, 0x003A))
    | {ord(char) for char in " .,:;!?-–—«»№()%"}
)

_WEIGHT_POINTS = (
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


def select_base_preset(parameters: FontParameters) -> str:
    """Выбирает эталонную гарнитуру по стилю, засечкам и назначению."""

    style = parameters.style.casefold()
    if parameters.serif:
        return "editorial"
    if any(token in style for token in ("cyber", "techno", "mono", "technical", "code", "кибер", "техно")):
        return "mono"
    if parameters.terminal_style == "angled" and parameters.sharpness >= 0.65:
        return "mono"
    if parameters.purpose == "screen":
        return "grotesk"
    if any(token in style for token in ("editorial", "book", "classic", "редакц", "книг")):
        return "editorial"
    return "grotesk"


def _axis_default_weight(font: TTFont) -> int:
    if "fvar" not in font:
        os2 = font.get("OS/2")
        return int(os2.usWeightClass) if os2 else 400
    axis = next((item for item in font["fvar"].axes if item.axisTag == "wght"), None)
    return int(round(axis.defaultValue)) if axis else 400


@lru_cache(maxsize=8)
def base_parameters_for_preset(preset_id: str) -> tuple[FontParameters, FontMetrics, str, bool]:
    """Нейтральные параметры эталонной гарнитуры для начального положения слайдеров."""

    if preset_id not in PRESETS:
        raise RequestError("Вариант шрифта не найден.")
    with read_font(preset_data(preset_id)) as font:
        analysis = analyze_font(font)
        weight = _axis_default_weight(font)
    parameters = analysis["parameters"]
    cap_height = min(780, max(500, parameters["cap_height"]))
    x_height = min(cap_height - 60, max(300, parameters["x_height"]))
    spec = BASE_FONT_SPECS[preset_id]
    result = FontParameters(
        weight=weight,
        width=100,
        slant=0,
        contrast=parameters["contrast"],
        roundness=0.0,
        sharpness=0.5,
        serif=spec["serif"],
        terminal_style="flat",
        stroke_contrast_axis="vertical",
        x_height=x_height,
        cap_height=cap_height,
        overshoot=True,
        ink_trap=False,
        mood="neutral",
        style="editorial" if spec["serif"] else ("techno" if preset_id == "mono" else "modern"),
        purpose="print",
        language_support=["latin", "cyrillic"],
    )
    metrics = FontMetrics(units_per_em=1000, ascender=800, descender=-200)
    return result, metrics, spec["family"], spec["serif"]


def analyze_bytes(data: bytes, base_font_id: str | None = None) -> dict:
    """Читает файл и возвращает профиль, пригодный для JSON-ответа."""

    with read_font(data) as font:
        return analyze_font(font, base_font_id)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _lerp_weight(ratio: float) -> int:
    if ratio <= _WEIGHT_POINTS[0][0]:
        return _WEIGHT_POINTS[0][1]
    if ratio >= _WEIGHT_POINTS[-1][0]:
        return _WEIGHT_POINTS[-1][1]
    for (left_ratio, left_weight), (right_ratio, right_weight) in zip(_WEIGHT_POINTS, _WEIGHT_POINTS[1:]):
        if left_ratio <= ratio <= right_ratio:
            span = right_ratio - left_ratio
            position = (ratio - left_ratio) / span if span else 0.0
            return int(round((left_weight + position * (right_weight - left_weight)) / 10.0) * 10)
    return 400


def _map_point(point, scale: float, width_factor: float, slant_tangent: float, vmap) -> tuple[float, float]:
    x = point[0] * scale * width_factor + point[1] * scale * slant_tangent
    y = vmap(point[1] * scale)
    return (x, y)


def _vertical_map_factory(source_x: float, source_cap: float, target_x: float, target_cap: float):
    if source_x <= 0 or source_cap <= 0 or source_cap <= source_x:
        return lambda value: value

    def mapper(value: float) -> float:
        if value < 0:
            return value * (target_cap / source_cap)
        if value <= source_x:
            return value * (target_x / source_x)
        if value <= source_cap:
            return target_x + (value - source_x) * (target_cap - target_x) / (source_cap - source_x)
        return target_cap + (value - source_cap) * (target_cap / source_cap)

    return mapper


def _corner_cut(previous, vertex, following, radius: float):
    ux, uy = previous[0] - vertex[0], previous[1] - vertex[1]
    wx, wy = following[0] - vertex[0], following[1] - vertex[1]
    length_u = math.hypot(ux, uy)
    length_w = math.hypot(wx, wy)
    if length_u < 1e-6 or length_w < 1e-6:
        return None
    cross = ux * wy - uy * wx
    if abs(cross) < 1e-6:
        return None
    distance = min(radius, 0.45 * length_u, 0.45 * length_w)
    if distance < 0.5:
        return None
    return (
        (vertex[0] + ux / length_u * distance, vertex[1] + uy / length_u * distance),
        (vertex[0] + wx / length_w * distance, vertex[1] + wy / length_w * distance),
    )


def _round_polyline(points, closed: bool, radius: float, bevel: bool):
    if len(points) < 3:
        return [("moveTo", (points[0],))] + [("lineTo", (point,)) for point in points[1:]] + (
            [("closePath", ())] if closed else []
        )
    if closed and points[0] == points[-1]:
        points = points[:-1]
    if len(points) < 3:
        return [("moveTo", (points[0],))] + [("lineTo", (point,)) for point in points[1:]] + (
            [("closePath", ())] if closed else []
        )

    if closed:
        cuts = [
            _corner_cut(points[index - 1], points[index], points[(index + 1) % len(points)], radius)
            for index in range(len(points))
        ]
        if not any(cuts):
            return [("moveTo", (points[0],))] + [("lineTo", (point,)) for point in points[1:]] + [("closePath", ())]
        start = cuts[0][1] if cuts[0] else points[0]
        commands = [("moveTo", (start,))]
        for index in range(1, len(points)):
            cut = cuts[index]
            if cut:
                commands.append(("lineTo", (cut[0],)))
                commands.append(("lineTo", (cut[1],)) if bevel else ("qCurveTo", (points[index], cut[1])))
            else:
                commands.append(("lineTo", (points[index],)))
        cut = cuts[0]
        if cut:
            commands.append(("lineTo", (cut[0],)))
            commands.append(("lineTo", (cut[1],)) if bevel else ("qCurveTo", (points[0], cut[1])))
        commands.append(("closePath", ()))
        return commands

    commands = [("moveTo", (points[0],))]
    for index in range(1, len(points) - 1):
        cut = _corner_cut(points[index - 1], points[index], points[index + 1], radius)
        if cut:
            commands.append(("lineTo", (cut[0],)))
            commands.append(("lineTo", (cut[1],)) if bevel else ("qCurveTo", (points[index], cut[1])))
        else:
            commands.append(("lineTo", (points[index],)))
    commands.append(("lineTo", (points[-1],)))
    return commands


def _round_commands(commands, radius: float, bevel: bool):
    if radius < 0.5:
        return commands
    result = []
    index = 0
    while index < len(commands):
        operator, args = commands[index]
        if operator != "moveTo":
            result.append((operator, args))
            index += 1
            continue
        points = [args[0]]
        cursor = index + 1
        while cursor < len(commands) and commands[cursor][0] == "lineTo":
            points.append(commands[cursor][1][0])
            cursor += 1
        closed = cursor < len(commands) and commands[cursor][0] in ("closePath", "endPath")
        result.extend(_round_polyline(points, closed, radius, bevel))
        index = cursor
        if closed:
            index += 1
    return result


def _path_from_commands(commands, mapper):
    if pathops is None:
        raise RequestError("Для обработки контуров установите skia-pathops.")
    path = pathops.Path()
    start = None
    for operator, args in commands:
        if operator == "moveTo":
            start = mapper(args[0])
            path.moveTo(*start)
        elif operator == "lineTo":
            path.lineTo(*mapper(args[0]))
        elif operator == "qCurveTo":
            points = list(args)
            end_raw = start if points[-1] is None else points[-1]
            controls = points[:-1]
            if not controls:
                path.lineTo(*mapper(end_raw))
                continue
            if len(controls) == 1:
                path.quadTo(*mapper(controls[0]), *mapper(end_raw))
            else:
                anchor = None
                for offset in range(len(controls) - 1):
                    midpoint = (
                        (controls[offset][0] + controls[offset + 1][0]) / 2.0,
                        (controls[offset][1] + controls[offset + 1][1]) / 2.0,
                    )
                    path.quadTo(*mapper(controls[offset]), *mapper(midpoint))
                    anchor = midpoint
                path.quadTo(*mapper(controls[-1]), *mapper(end_raw))
        elif operator == "curveTo":
            points = [mapper(point) for point in args]
            for offset in range(0, len(points), 3):
                first, second, end = points[offset : offset + 3]
                path.cubicTo(*first, *second, *end)
        elif operator in ("closePath", "endPath"):
            path.close()
    return path


def _simplify(path):
    if pathops is None:
        return path
    try:
        return pathops.simplify(path)
    except Exception:
        return path


def _extract_segments(path, orientation: str):
    output = pathops.Path()
    x_min, y_min, x_max, y_max = path.bounds
    edge_epsilon = 2.0

    def is_extreme(start_point, end_point):
        if orientation == "horizontal":
            return (
                abs(start_point[1] - y_min) <= edge_epsilon
                or abs(start_point[1] - y_max) <= edge_epsilon
                or abs(end_point[1] - y_min) <= edge_epsilon
                or abs(end_point[1] - y_max) <= edge_epsilon
            )
        return (
            abs(start_point[0] - x_min) <= edge_epsilon
            or abs(start_point[0] - x_max) <= edge_epsilon
            or abs(end_point[0] - x_min) <= edge_epsilon
            or abs(end_point[0] - x_max) <= edge_epsilon
        )

    for contour in path.contours:
        current = None
        start = None
        for operator, points in contour.segments:
            if operator == "moveTo":
                current = points[0]
                start = current
            elif operator == "lineTo":
                end = points[0]
                if current is not None:
                    dx = abs(end[0] - current[0])
                    dy = abs(end[1] - current[1])
                    if (
                        (orientation == "horizontal" and dy <= dx * 0.25 and dx > 2)
                        or (orientation == "vertical" and dx <= dy * 0.25 and dy > 2)
                    ) and not is_extreme(current, end):
                        output.moveTo(*current)
                        output.lineTo(*end)
                current = end
            elif operator in ("quadTo", "cubicTo", "conicTo"):
                current = points[-1] if points else current
            elif operator == "close":
                if current is not None and start is not None:
                    dx = abs(start[0] - current[0])
                    dy = abs(start[1] - current[1])
                    if (
                        (orientation == "horizontal" and dy <= dx * 0.25 and dx > 2)
                        or (orientation == "vertical" and dx <= dy * 0.25 and dy > 2)
                    ) and not is_extreme(current, start):
                        output.moveTo(*current)
                        output.lineTo(*start)
                current = start
    return output


def _apply_contrast(path, delta: float, axis: str):
    if pathops is None or abs(delta) < 0.5 or axis not in ("vertical", "horizontal"):
        return path
    orientation = "horizontal" if axis == "vertical" else "vertical"
    segments = _extract_segments(path, orientation)
    if not list(segments.segments):
        return path
    try:
        stroke = pathops.Path()
        stroke.addPath(segments)
        stroke.stroke(2.0 * abs(delta), pathops.LineCap.BUTT_CAP, pathops.LineJoin.MITER_JOIN, 4.0)
        stroke.convertConicsToQuads(1.0)
        operation = pathops.PathOp.UNION if delta > 0 else pathops.PathOp.DIFFERENCE
        return _simplify(pathops.op(path, stroke, operation))
    except Exception:
        return path


def _offset_path(path, delta: float):
    if pathops is None or abs(delta) < 0.5:
        return path
    try:
        stroke = pathops.Path()
        stroke.addPath(path)
        stroke.stroke(2.0 * abs(delta), pathops.LineCap.BUTT_CAP, pathops.LineJoin.MITER_JOIN, 4.0)
        stroke.convertConicsToQuads(1.0)
        operation = pathops.PathOp.UNION if delta > 0 else pathops.PathOp.DIFFERENCE
        return _simplify(pathops.op(path, stroke, operation))
    except Exception:
        return path


def _draw_commands_to_pen(commands, mapper, pen):
    """Запасной путь без skia-pathops: только аффинные преобразования и кривые."""

    for operator, args in commands:
        if operator == "moveTo":
            pen.moveTo(mapper(args[0]))
        elif operator == "lineTo":
            pen.lineTo(mapper(args[0]))
        elif operator == "qCurveTo":
            pen.qCurveTo(*(None if point is None else mapper(point) for point in args))
        elif operator == "curveTo":
            pen.curveTo(*(mapper(point) for point in args))
        elif operator == "closePath":
            pen.closePath()
        elif operator == "endPath":
            pen.endPath()


def _draw_path_to_pen(path, pen):
    """Явно начинает каждый контур с moveTo (pathops опускает его у первого контура)."""

    for contour in path.contours:
        starts = contour.firstPoints
        if starts:
            pen.moveTo(starts[0])
        for operator, points in contour.segments:
            if operator == "moveTo":
                if not starts:
                    pen.moveTo(points[0])
            elif operator == "lineTo":
                pen.lineTo(points[0])
            elif operator == "qCurveTo":
                pen.qCurveTo(*points)
            elif operator == "curveTo":
                pen.curveTo(*points)
            elif operator == "closePath":
                pen.closePath()
            elif operator == "endPath":
                pen.endPath()


def _normalize_directions(path):
    if pathops is None:
        return path
    try:
        contours = list(path.contours)
        normalized = pathops.Path()
        for index, contour in enumerate(contours):
            first_point = list(contour.segments)[0][1][0]
            depth = sum(
                1
                for other_index, other in enumerate(contours)
                if other_index != index and other.contains(first_point)
            )
            want_clockwise = depth % 2 == 0
            if bool(contour.clockwise) != want_clockwise:
                contour.reverse()
            normalized.add(contour)
        return normalized
    except Exception:
        return path


def _autohint_ttf(data: bytes) -> bytes:
    executable = shutil.which("ttfautohint")
    if not executable:
        return data
    try:
        with tempfile.TemporaryDirectory() as directory:
            source = f"{directory}/in.ttf"
            target = f"{directory}/out.ttf"
            with open(source, "wb") as handle:
                handle.write(data)
            subprocess.run(
                [executable, "--no-info", source, target],
                check=True,
                capture_output=True,
                timeout=20,
            )
            with open(target, "rb") as handle:
                return handle.read()
    except Exception:
        return data


def _width_class(width: int) -> int:
    if width <= 62:
        return 1
    if width <= 75:
        return 2
    if width <= 87:
        return 3
    if width <= 100:
        return 4
    if width <= 112:
        return 5
    if width <= 125:
        return 6
    if width <= 150:
        return 7
    if width <= 175:
        return 8
    return 9


def _family_name(value: str) -> str:
    name = unicodedata.normalize("NFC", value or "").strip() or "AI Font"
    if len(name) > 60 or any(unicodedata.category(char).startswith("C") for char in name):
        raise RequestError("Введите название шрифта длиной до 60 символов.")
    return name


def generate_font(
    parameters,
    metrics=None,
    *,
    base_font_id: str | None = None,
    base_parameters=None,
    base_data: bytes | None = None,
    family_name: str = "AI Font",
    tracking: float = 0.0,
    output_format: str = "ttf",
    autohint: bool = True,
    license_text: str | None = None,
) -> bytes:
    """Собирает TTF из эталонного или загруженного шрифта."""

    parameters = parameters if isinstance(parameters, FontParameters) else FontParameters.model_validate(parameters)
    metrics = metrics if isinstance(metrics, FontMetrics) else FontMetrics.model_validate(metrics or {})
    if base_parameters is not None and not isinstance(base_parameters, FontParameters):
        base_parameters = FontParameters.model_validate(base_parameters)

    preset_id = None
    if base_data is not None:
        data = base_data
    elif base_font_id:
        try:
            data = load_font(base_font_id)
        except ValueError as exc:
            raise RequestError(str(exc)) from exc
    else:
        preset_id = select_base_preset(parameters)
        data = preset_data(preset_id)

    with read_font(data) as font:
        if base_parameters is None:
            if preset_id:
                base_parameters = base_parameters_for_preset(preset_id)[0]
            else:
                base_parameters = FontParameters.model_validate(analyze_font(font)["parameters"])

        source_upem = font["head"].unitsPerEm or 1000
        scale = 1000.0 / source_upem

        weight_axis = None
        extra_weight = parameters.weight - base_parameters.weight
        if "fvar" in font:
            axes = font["fvar"].axes
            weight_axis = next((axis for axis in axes if axis.axisTag == "wght"), None)
            if weight_axis:
                clamped = _clamp(parameters.weight, weight_axis.minValue, weight_axis.maxValue)
                coordinates = {axis.axisTag: axis.defaultValue for axis in axes}
                coordinates["wght"] = clamped
                try:
                    instantiateVariableFont(font, coordinates, inplace=True)
                except Exception:
                    pass
                extra_weight = parameters.weight - clamped

        width_factor = parameters.width / base_parameters.width if base_parameters.width else 1.0
        width_factor = _clamp(width_factor, 0.35, 3.0)
        slant_extra = parameters.slant - base_parameters.slant
        slant_tangent = math.tan(math.radians(_clamp(slant_extra, -30, 30)))

        source_x = base_parameters.x_height
        source_cap = base_parameters.cap_height
        target_x = parameters.x_height
        target_cap = parameters.cap_height
        vmap = _vertical_map_factory(source_x, source_cap, target_x, target_cap)

        base_stem = max(20.0, 0.09 * 1000.0)
        radius = parameters.roundness * 0.30 * base_stem
        radius *= 1.0 - 0.45 * parameters.sharpness
        if parameters.terminal_style == "rounded":
            radius += 0.08 * base_stem
        bevel = parameters.terminal_style == "angled"
        contrast_delta = parameters.contrast - base_parameters.contrast
        # Высокий контраст означает более тонкие горизонтальные штрихи.
        contrast_units = _clamp(-contrast_delta * 0.06 * 1000.0, -60.0, 60.0)
        extra_weight_units = _clamp(extra_weight * 0.00016 * 1000.0, -35.0, 45.0)
        tracking_units = tracking

        glyph_set = font.getGlyphSet()
        glyph_order = font.getGlyphOrder()
        cmap = font.getBestCmap() or {}
        required_names = {cmap[code] for code in REQUIRED_CODES if code in cmap}
        glyphs = {}
        horizontal_metrics = {}
        expensive_enabled = pathops is not None

        for glyph_name in glyph_order:
            advance, bearing = font["hmtx"][glyph_name]
            new_advance = round(advance * scale * width_factor + (tracking_units if advance > 0 else 0))
            new_advance = max(0, new_advance)

            recorder = DecomposingRecordingPen(glyph_set)
            glyph_set[glyph_name].draw(recorder)
            commands = recorder.value
            if not commands:
                glyphs[glyph_name] = TTGlyphPen(None).glyph()
                horizontal_metrics[glyph_name] = (new_advance, 0)
                continue

            if glyph_name in required_names and radius > 0.5:
                commands = _round_commands(commands, radius * source_upem / 1000.0, bevel)

            mapper = lambda point: _map_point(point, scale, width_factor, slant_tangent, vmap)
            pen = TTGlyphPen(None)
            try:
                if pathops is None:
                    _draw_commands_to_pen(
                        commands,
                        mapper,
                        Cu2QuPen(pen, 1.0, reverse_direction="glyf" not in font),
                    )
                else:
                    path = _path_from_commands(commands, mapper)
                    path = _simplify(path)
                    if glyph_name in required_names:
                        path = _apply_contrast(path, contrast_units, parameters.stroke_contrast_axis)
                        path = _offset_path(path, extra_weight_units)
                    path = _normalize_directions(path)
                    _draw_path_to_pen(path, Cu2QuPen(pen, 1.0, reverse_direction=False))
            except RequestError:
                raise
            except Exception as exc:
                raise RequestError("Не удалось собрать контуры глифа.") from exc
            glyph = pen.glyph()
            glyph.recalcBounds(None)
            glyphs[glyph_name] = glyph
            horizontal_metrics[glyph_name] = (
                new_advance,
                glyph.xMin if glyph.numberOfContours else round(bearing * scale * width_factor),
            )

    family = _family_name(family_name)
    postscript = re.sub(r"[^A-Za-z0-9-]", "", family.replace(" ", "-")).strip("-")[:40] or "AIFont"
    postscript += "-" + uuid.uuid4().hex[:8]

    names = {
        "familyName": family,
        "styleName": "Regular",
        "uniqueFontIdentifier": postscript,
        "fullName": f"{family} Regular",
        "psName": postscript,
        "version": "Version 1.000",
        "licenseDescription": license_text or "Сгенерировано в студии «Шрифты от ИИ».",
        "licenseInfoURL": "https://openfontlicense.org",
    }

    has_cyrillic = any(0x0400 <= code <= 0x04FF for code in cmap)
    has_latin = any(0x0041 <= code <= 0x007A for code in cmap)
    code_pages = (1 << 0) | (1 << 2) if has_cyrillic else (1 << 0)
    unicode_ranges = 1 << 0
    if has_cyrillic:
        unicode_ranges |= 1 << 9
    if any(0x00A0 <= code <= 0x00FF for code in cmap):
        unicode_ranges |= 1 << 1
    if any(0x2000 <= code <= 0x206F for code in cmap):
        unicode_ranges |= 1 << 31

    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap(cmap)
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics(horizontal_metrics)
    builder.setupHorizontalHeader(
        ascent=metrics.ascender,
        descent=metrics.descender,
        lineGap=0,
        caretSlopeRise=1000,
        caretSlopeRun=round(1000 * math.tan(math.radians(_clamp(parameters.slant, -15, 15)))),
    )
    builder.setupNameTable(names)
    builder.setupOS2(
        sTypoAscender=metrics.ascender,
        sTypoDescender=metrics.descender,
        sTypoLineGap=0,
        usWinAscent=max(0, metrics.ascender),
        usWinDescent=max(0, -metrics.descender),
        usWeightClass=max(1, min(1000, int(parameters.weight))),
        usWidthClass=_width_class(parameters.width),
        fsType=0,
        achVendID="AIFN",
        sxHeight=max(0, min(1000, parameters.x_height)),
        sCapHeight=max(0, min(1000, parameters.cap_height)),
        ulCodePageRange1=code_pages,
        ulUnicodeRange1=unicode_ranges,
    )
    builder.setupPost(
        italicAngle=-parameters.slant,
        isFixedPitch=int(len({advance for advance, _ in horizontal_metrics.values() if advance}) == 1),
    )
    builder.font["head"].macStyle = (1 if parameters.weight >= 700 else 0) | (2 if parameters.slant else 0)
    builder.font["head"].fontRevision = 1.0
    for name_id, value in ((16, family), (17, "Regular")):
        builder.font["name"].setName(value, name_id, 3, 1, 0x409)

    output = BytesIO()
    if output_format == "woff2":
        builder.font.flavor = "woff2"
    try:
        builder.save(output)
    except Exception:
        if builder.font.flavor != "woff2":
            raise
        builder.font.flavor = None
        output = BytesIO()
        builder.save(output)
    result = output.getvalue()
    if autohint and output_format == "ttf":
        result = _autohint_ttf(result)
    return result


def license_for_preset(preset_id: str | None) -> str | None:
    if not preset_id:
        return None
    path = FONT_DIR / f"OFL-{preset_id}.txt"
    return path.read_text(encoding="utf-8") if path.is_file() else None
