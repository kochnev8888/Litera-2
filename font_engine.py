import math
import re
import unicodedata
import uuid
from io import BytesIO
from pathlib import Path

from fontTools.fontBuilder import FontBuilder
from fontTools.misc.transform import Transform
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont


FONT_DIR = Path(__file__).resolve().parent / "static" / "fonts"
MAX_FONT_BYTES = 10 * 1024 * 1024

PRESETS = {
    "editorial": {
        "id": "editorial", "title": "Редакционный", "category": "Антиква",
        "keywords": ("засеч", "редакц", "журнал", "книг", "элегант", "газет", "печат", "serif"),
    },
    "grotesk": {
        "id": "grotesk", "title": "Геометрический", "category": "Гротеск",
        "keywords": ("геометр", "гротеск", "минимал", "интерфейс", "нейтрал", "соврем", "sans", "без засеч"),
    },
    "mono": {
        "id": "mono", "title": "Технический", "category": "Моноширинный",
        "keywords": ("моно", "техни", "код", "терминал", "пиксел", "машин", "mono"),
    },
}

RUSSIAN = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
ENGLISH = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
SYMBOLS = "0123456789.,!?-–—:;«»“”()[]&@#%+×÷=₽$€№"
SETS = {"cyrillic": RUSSIAN + RUSSIAN.lower(), "latin": ENGLISH + ENGLISH.lower(), "symbols": SYMBOLS}


class RequestError(ValueError):
    pass


def read_font(data):
    if data[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        raise RequestError("Нужен файл .ttf или .otf с векторными контурами.")
    try:
        font = TTFont(BytesIO(data), lazy=False)
        if not all(tag in font for tag in ("head", "hhea", "hmtx", "maxp", "name", "cmap")):
            raise ValueError("missing metrics or mapping")
        if not any(tag in font for tag in ("glyf", "CFF ", "CFF2")):
            raise ValueError("missing outlines")
        if any(tag in font for tag in ("COLR", "SVG ", "sbix", "CBDT")):
            raise RequestError("Поддерживаются одноцветные TTF и OTF.")
        if not font.getBestCmap() or font["maxp"].numGlyphs > 12000:
            raise ValueError("unsupported cmap size")
        return font
    except RequestError:
        raise
    except Exception as exc:
        raise RequestError("Файл повреждён или содержит неподдерживаемую структуру.") from exc


def preset_data(preset_id):
    if preset_id not in PRESETS:
        raise RequestError("Вариант шрифта не найден.")
    return (FONT_DIR / f"{preset_id}.ttf").read_bytes()


def describe(font):
    cmap = font.getBestCmap()
    name_table = font["name"]
    axis = next((a for a in font["fvar"].axes if a.axisTag == "wght"), None) if "fvar" in font else None
    return {
        "family": name_table.getDebugName(16) or name_table.getDebugName(1) or "Без названия",
        "style": name_table.getDebugName(17) or name_table.getDebugName(2) or "Regular",
        "format": "TTF" if "glyf" in font else "OTF",
        "glyphCount": len(font.getGlyphOrder()),
        "unicodeCount": len(cmap),
        "cyrillicBlockCount": sum(i in cmap for i in range(0x400, 0x500)),
        "sets": {key: "".join(char for char in chars if ord(char) in cmap) for key, chars in SETS.items()},
        "weight": {"min": axis.minValue, "max": axis.maxValue, "default": axis.defaultValue} if axis else None,
        "restrictedEmbedding": bool(font["OS/2"].fsType & 0x0002) if "OS/2" in font else False,
    }


def recommend(prompt):
    """Integration point for the team's text-to-font model."""
    def relevance(preset):
        query = re.sub(r"без\s+засеч\w*|sans[- ]serif", "", prompt) if preset["id"] == "editorial" else prompt
        return sum(term in query for term in preset["keywords"])

    return sorted(PRESETS.values(), key=relevance, reverse=True)


def preset_option(preset):
    with read_font(preset_data(preset["id"])) as font:
        return {key: preset[key] for key in ("id", "title", "category")} | {"font": describe(font)}


def number(params, key, default, minimum, maximum):
    try:
        result = float(params.get(key, default))
    except (ValueError, TypeError) as exc:
        raise RequestError("Параметр шрифта должен быть числом.") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise RequestError("Значение параметра выходит за пределы диапазона.")
    return result


def export_font(font, params, license_text=None):
    width = number(params, "width", 100, 75, 125) / 100
    tracking = number(params, "tracking", 0, -35, 100) / 1000
    slant = number(params, "slant", 0, -12, 16)
    family = unicodedata.normalize("NFC", params.get("name", "").strip()) or "Litera Draft"
    if len(family) > 60 or any(unicodedata.category(char).startswith("C") for char in family):
        raise RequestError("Введите название шрифта длиной до 60 символов.")
    if license_text:
        reserved_names = re.findall(r'Reserved Font Name\s+"([^"]+)"', license_text, re.IGNORECASE)
        if any(name.casefold() in family.casefold() for name in reserved_names):
            raise RequestError("Введите новое название без зарезервированного имени исходного шрифта.")

    axes = font["fvar"].axes if "fvar" in font else []
    weight_axis = next((axis for axis in axes if axis.axisTag == "wght"), None)
    old_os2 = font.get("OS/2")
    weight = old_os2.usWeightClass if old_os2 else 400
    if weight_axis:
        weight = number(params, "weight", weight_axis.defaultValue, weight_axis.minValue, weight_axis.maxValue)
    if axes:
        coordinates = {axis.axisTag: axis.defaultValue for axis in axes}
        if weight_axis:
            coordinates["wght"] = weight
        instantiateVariableFont(font, coordinates, inplace=True)

    glyph_set = font.getGlyphSet()
    glyph_order = font.getGlyphOrder()
    units = font["head"].unitsPerEm
    space = tracking * units
    transform = Transform(width, 0, math.tan(math.radians(slant)), 1)
    glyphs, metrics = {}, {}
    for glyph_name in glyph_order:
        advance, bearing = font["hmtx"][glyph_name]
        added_space = space if advance > 0 else 0
        outline = DecomposingRecordingPen(glyph_set)
        glyph_set[glyph_name].draw(outline)
        pen = TTGlyphPen(None)
        outline.replay(TransformPen(Cu2QuPen(pen, units / 2000, reverse_direction="glyf" not in font),
                                    transform.translate(added_space / (2 * width), 0)))
        glyph = pen.glyph()
        glyph.recalcBounds(None)
        glyphs[glyph_name] = glyph
        new_advance = max(1, round(advance * width + added_space)) if advance > 0 else 0
        new_bearing = glyph.xMin if glyph.numberOfContours else round(bearing * width)
        metrics[glyph_name] = (new_advance, new_bearing)

    postscript = re.sub(r"[^A-Za-z0-9-]", "", family.replace(" ", "-")).strip("-")[:40] or "Litera"
    # Distinguish exports based on the same family name in OS font caches.
    postscript += "-" + uuid.uuid4().hex[:8]
    names = {
        "familyName": family, "styleName": "Regular", "uniqueFontIdentifier": postscript,
        "fullName": family, "psName": postscript, "version": "Version 1.000",
    }
    for key, name_id in (("copyright", 0), ("licenseDescription", 13), ("licenseInfoURL", 14)):
        value = font["name"].getDebugName(name_id)
        if value:
            names[key] = value
    if license_text:
        names["licenseDescription"] = license_text

    hhea = font["hhea"]
    builder = FontBuilder(units, isTTF=True)
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap(font.getBestCmap())
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics(metrics)
    builder.setupHorizontalHeader(ascent=hhea.ascent, descent=hhea.descent, lineGap=hhea.lineGap)
    builder.setupNameTable(names)
    builder.setupOS2(
        sTypoAscender=old_os2.sTypoAscender if old_os2 else hhea.ascent,
        sTypoDescender=old_os2.sTypoDescender if old_os2 else hhea.descent,
        sTypoLineGap=old_os2.sTypoLineGap if old_os2 else hhea.lineGap,
        usWinAscent=old_os2.usWinAscent if old_os2 else max(0, hhea.ascent),
        usWinDescent=old_os2.usWinDescent if old_os2 else max(0, -hhea.descent),
        usWeightClass=max(1, min(1000, round(weight))),
        fsType=old_os2.fsType if old_os2 else 0,
    )
    builder.setupPost(isFixedPitch=int(len({advance for advance, _ in metrics.values() if advance}) == 1))
    output = BytesIO()
    builder.save(output)
    return output.getvalue()
