import unittest
from io import BytesIO

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen

from font_engine import (
    FONT_DIR, PRESETS, SETS, RequestError, describe, export_font, preset_data, read_font, recommend,
)


class FontEngineTest(unittest.TestCase):
    def test_prompt_selects_relevant_style(self):
        prompts = {
            "антиква с засечками для журнала": "editorial",
            "лёгкий шрифт без засечек": "grotesk",
            "моноширинный для терминала": "mono",
        }
        for prompt, expected in prompts.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(recommend(prompt)[0]["id"], expected)

    def test_presets_compile_with_the_requested_characters(self):
        for preset in PRESETS:
            with self.subTest(preset=preset), read_font(preset_data(preset)) as source:
                cmap = source.getBestCmap()
                license_text = (FONT_DIR / f"OFL-{preset}.txt").read_text(encoding="utf-8")
                result = export_font(source, {"name": "Литера тест", "width": 110, "tracking": 20, "weight": 650}, license_text)
                with read_font(result) as generated:
                    glyph = cmap[ord("А")]
                    self.assertEqual(generated.getBestCmap(), cmap)
                    self.assertEqual(describe(generated)["sets"], SETS)
                    self.assertEqual(generated["name"].getDebugName(1), "Литера тест")
                    self.assertIn("SIL OPEN FONT LICENSE", generated["name"].getDebugName(13))
                    self.assertEqual(generated["OS/2"].usWeightClass, 650)
                    self.assertGreater(generated["hmtx"][glyph][0], source["hmtx"][glyph][0])
                    self.assertNotIn("fvar", generated)

    def test_otf_with_cubic_contours_reexports_as_ttf(self):
        builder = FontBuilder(1000, isTTF=False)
        builder.setupGlyphOrder([".notdef", "A"])
        builder.setupCharacterMap({ord("A"): "A", ord("А"): "A"})
        pen = T2CharStringPen(600, None)
        notdef = pen.getCharString()
        pen = T2CharStringPen(600, None)
        pen.moveTo((50, 0))
        pen.curveTo((100, 700), (450, 700), (550, 0))
        pen.closePath()
        builder.setupCFF("Demo-OTF", {"FullName": "Demo OTF"}, {".notdef": notdef, "A": pen.getCharString()}, {})
        builder.setupHorizontalMetrics({".notdef": (600, 0), "A": (600, 50)})
        builder.setupHorizontalHeader(ascent=800, descent=-200)
        builder.setupNameTable({"familyName": "Demo OTF", "styleName": "Regular"})
        builder.setupOS2()
        builder.setupPost()
        raw = BytesIO()
        builder.save(raw)

        with read_font(raw.getvalue()) as source:
            self.assertEqual(describe(source)["format"], "OTF")
            original = export_font(source, {})
        with read_font(raw.getvalue()) as source:
            modified = export_font(source, {"width": 115, "tracking": 30, "slant": 9})
        with read_font(original) as base, read_font(modified) as generated:
            self.assertEqual(describe(generated)["format"], "TTF")
            self.assertEqual(generated.getBestCmap(), base.getBestCmap())
            self.assertEqual(generated["hmtx"]["A"][0], 720)
            self.assertNotEqual(generated["glyf"]["A"].coordinates, base["glyf"]["A"].coordinates)

    def test_invalid_input(self):
        with self.assertRaises(RequestError):
            read_font(b"bad format")
        with read_font(preset_data("editorial")) as font:
            with self.assertRaises(RequestError):
                export_font(font, {"width": "NaN"})
