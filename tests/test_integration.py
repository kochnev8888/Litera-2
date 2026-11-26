import os
import unittest
from io import BytesIO
from unittest import mock

# Тесты не должны зависеть от запущенной Ollama и внешних моделей.
os.environ.setdefault("FONT_AI_DISABLE_LLM", "1")

from fontTools.ttLib import TTFont

from ai_service import generate_variants
from font_analyzer import analyze_font
from font_engine import preset_data, read_font
from font_schema import FontMetrics, FontParameters
from font_synthesis import analyze_bytes, base_parameters_for_preset, generate_font


class AiServiceTest(unittest.TestCase):
    def test_llm_defaults_to_local_ollama_qwen(self):
        from ai_service import _llm_config

        with mock.patch.dict(os.environ, {}, clear=True):
            config = _llm_config()
        self.assertIsNotNone(config)
        self.assertEqual(config["provider"], "ollama")
        self.assertEqual(config["model"], "qwen2.5:3b")
        self.assertIn("11434", config["base_url"])

    def test_heuristic_returns_distinct_variants_with_fixed_grid(self):
        mode, variants = generate_variants("агрессивный узкий киберпанк для плаката", 3)
        self.assertIn(mode, {"heuristic", "llm"})
        self.assertEqual(len(variants), 3)
        signatures = {variant.parameters.model_dump_json() for variant in variants}
        self.assertEqual(len(signatures), 3)
        for variant in variants:
            self.assertEqual(variant.metrics, FontMetrics())
            self.assertLess(variant.parameters.x_height, variant.parameters.cap_height)
            self.assertLessEqual(variant.parameters.cap_height, variant.metrics.ascender)

    def test_prompt_semantics_are_mapped(self):
        _, variants = generate_variants("элегантная антиква с засечками для книги", 1)
        self.assertTrue(variants[0].parameters.serif)
        _, variants = generate_variants("моноширинный технический шрифт для кода", 1)
        self.assertFalse(variants[0].parameters.serif)
        self.assertIn(variants[0].parameters.style, {"techno", "cyberpunk"})


class FontSynthesisTest(unittest.TestCase):
    def test_generate_builds_print_ready_tables(self):
        parameters, metrics, _, _ = base_parameters_for_preset("grotesk")
        parameters = parameters.model_copy(
            update={"weight": 700, "width": 90, "slant": 6, "contrast": 0.5, "roundness": 0.4}
        )
        data = generate_font(parameters, metrics, family_name="Integration Test", autohint=False)
        with TTFont(BytesIO(data)) as font:
            for tag in ("head", "hhea", "maxp", "OS/2", "name", "cmap", "post", "glyf", "hmtx"):
                self.assertIn(tag, font)
            self.assertEqual(font["OS/2"].usWeightClass, 700)
            self.assertEqual(font["post"].italicAngle, -6)
            self.assertEqual(font["OS/2"].sxHeight, parameters.x_height)
            self.assertEqual(font["OS/2"].sCapHeight, parameters.cap_height)
            self.assertTrue(font["OS/2"].ulCodePageRange1 & (1 << 2))
            self.assertEqual(font["name"].getDebugName(1), "Integration Test")
            cmap = font.getBestCmap()
            for character in "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюяABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789":
                self.assertIn(ord(character), cmap)

    def test_analyzer_detects_serif_and_roundtrips_metrics(self):
        with read_font(preset_data("editorial")) as font:
            profile = analyze_font(font)
        self.assertTrue(profile["parameters"]["serif"])
        self.assertGreater(profile["parameters"]["contrast"], 0.3)

        with read_font(preset_data("grotesk")) as font:
            profile = analyze_font(font)
        self.assertFalse(profile["parameters"]["serif"])

        parameters, metrics, _, _ = base_parameters_for_preset("mono")
        parameters = parameters.model_copy(update={"weight": 600, "slant": 4, "x_height": 540})
        data = generate_font(parameters, metrics, family_name="Roundtrip", autohint=False)
        detected = analyze_bytes(data)
        self.assertEqual(detected["parameters"]["weight"], 600)
        self.assertEqual(detected["parameters"]["slant"], 4)
        self.assertEqual(detected["parameters"]["x_height"], 540)
        self.assertEqual(detected["parameters"]["cap_height"], parameters.cap_height)

    def test_custom_metrics_are_respected(self):
        parameters = FontParameters(
            weight=500,
            width=95,
            slant=0,
            contrast=0.3,
            roundness=0.2,
            sharpness=0.5,
            serif=False,
            terminal_style="flat",
            stroke_contrast_axis="vertical",
            x_height=530,
            cap_height=740,
            overshoot=True,
            ink_trap=False,
            mood="neutral",
            style="modern",
            purpose="print",
            language_support=["latin", "cyrillic"],
        )
        metrics = FontMetrics(units_per_em=1000, ascender=820, descender=-180)
        data = generate_font(parameters, metrics, family_name="Custom Metrics", autohint=False)
        with TTFont(BytesIO(data)) as font:
            self.assertEqual(font["hhea"].ascent, 820)
            self.assertEqual(font["hhea"].descent, -180)
            self.assertEqual(font["OS/2"].sTypoAscender, 820)
            self.assertEqual(font["OS/2"].sTypoDescender, -180)


if __name__ == "__main__":
    unittest.main()
