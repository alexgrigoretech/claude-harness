import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import check_design


class CheckDesignTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.design = self.write("DESIGN.md", "\n".join([
            "---",
            "color:",
            "  primary: {500: '#123456'}",
            "  neutral: {50: '#abc'}",
            "  semantic: {surface: neutral.50, missing: neutral.999}",
            "spacing: {scale: [4, 8, 16, 24]}",
            "radius: {sm: 4, full: 9999}",
            "type:",
            "  display: {family: 'Specimen Display'}",
            "  body: {family: 'Specimen Text'}",
            "  scale: {sm: 12, md: 16}",
            "icons: specimen-icons",
            "motion: {reduced_motion: required}",
            "---",
            "Prose below the front matter.",
        ]))
        self.tokens = check_design.read_design(self.design)

    def write(self, relative, content):
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def inspect(self, content, rule, suffix=".css", tokens=True):
        path = self.write("sample" + suffix, content)
        return check_design.inspect_file(path, {rule}, self.tokens if tokens else None)

    def invoke(self, arguments, discovery=False):
        out = io.StringIO()
        err = io.StringIO()
        args = arguments if discovery else ["--design", str(self.design), *arguments]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = check_design.main(args)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_extract_tokens(self):
        self.assertEqual(self.tokens["colors"], {"#123456", "#aabbcc"})
        self.assertEqual(self.tokens["spacing"], {4, 8, 16, 24})
        self.assertEqual(self.tokens["radius"], {"sm": 4, "full": 9999})
        self.assertEqual(self.tokens["type_scale"], {"sm": 12, "md": 16})
        self.assertEqual(self.tokens["families"], {"specimen display", "specimen text"})
        self.assertEqual(self.tokens["icons"], "specimen-icons")
        self.assertEqual(self.tokens["reduced_motion"], "required")

    def test_color_alias_cycles_are_ignored(self):
        path = self.write("aliases.md", "---\ncolor: {a: b, b: a, c: missing}\n---\n")
        self.assertEqual(check_design.read_design(path)["colors"], set())

    def test_design_functional_colors_and_aliases(self):
        colors = {
            "ink": {500: "OKLCH( .7  .22  292 )"},
            "rgb": "rgb(1, 2, 3)", "rgba": "rgba(1, 2, 3, .5)",
            "hsl": "hsl(90 50% 50%)", "hsla": "HSLA(90, 50%, 50%, .5)",
            "hex": "#ABCD", "semantic": {"action": "ink.500"},
            "ignored": "red", "invalid": "rgb(1,2,3) trailing", "number": 123,
        }
        import yaml
        self.design.write_text("---\n" + yaml.safe_dump({"color": colors}) + "---\n", encoding="utf-8")
        self.tokens = check_design.read_design(self.design)
        expected = {"oklch(0.7 0.22 292)", "rgb(1,2,3)", "rgba(1,2,3,0.5)", "hsl(90 50% 50%)", "hsla(90,50%,50%,0.5)", "#aabbccdd"}
        self.assertEqual(self.tokens["colors"], expected)
        for color in expected:
            with self.subTest(color=color):
                self.assertEqual(self.inspect(f".sample {{ color: {color}; }}", "raw-color"), [])

    def test_oklch_spelling_matches_both_directions(self):
        spellings = ("oklch(0.70 0.220 292)", "oklch(0.7 0.22 292)", "OKLCH( .7  .22  292 )")
        for token in spellings:
            self.design.write_text(f'---\ncolor: {{ink: {{500: "{token}"}}, semantic: {{action: ink.500}}}}\n---\n', encoding="utf-8")
            self.tokens = check_design.read_design(self.design)
            for literal in spellings:
                with self.subTest(token=token, literal=literal):
                    self.assertEqual(self.inspect(f".sample {{ color: {literal}; }}", "raw-color"), [])
            self.assertTrue(self.inspect(".sample { color: oklch(0.5 0.1 100); }", "raw-color"))

    def test_canonical_color_hex(self):
        for value, expected in ((" #ABC ", "#aabbcc"), ("#AbCd", "#aabbccdd"), ("#12345678", "#12345678")):
            with self.subTest(value=value):
                self.assertEqual(check_design.canonical_color(value), expected)
        self.assertEqual(check_design.normalize_hex("#ABCD"), "#aabbccdd")

    def test_canonical_color_functions(self):
        cases = (
            (" OKLCH( .7  .220\t292 ) ", "oklch(0.7 0.22 292)"),
            ("RGBA(255, 255 , 255, .08)", "rgba(255,255,255,0.08)"),
            ("rgb( 00  +3  1.0 / 40.00% )", "rgb(0 3 1/40%)"),
            ("HSL( -03.00deg 050.00% 50.0% / .50 )", "hsl(-3deg 50% 50%/0.5)"),
            ("hsla(1.20e2, 050%, 50%, 1.)", "hsla(120,50%,50%,1)"),
            ("oklch(0.1234567890123456789 0.000 -00)", "oklch(0.1234567890123456789 0 -0)"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(check_design.canonical_color(value), expected)

    def test_canonical_color_notations_stay_distinct(self):
        self.tokens["colors"] = {check_design.canonical_color("rgba(1, 2, 3, .08)")}
        self.assertEqual(self.inspect(".sample { color: rgba(1,2,3,0.08); }", "raw-color"), [])
        self.assertTrue(self.inspect(".sample { color: rgb(1 2 3 / 0.08); }", "raw-color"))
        self.assertNotEqual(check_design.canonical_color("rgb(255 255 255 / 0.08)"),
                            check_design.canonical_color("rgba(255,255,255,0.08)"))

    def test_canonical_color_large_exponents(self):
        start = time.perf_counter()
        for number in ("1e999999999", "1e-999999999", "1e7", "1e-7", "+1.00E+999999999"):
            with self.subTest(number=number):
                self.assertEqual(check_design.canonical_color(f"rgb({number} 0 0)"), f"rgb({number.lower()} 0 0)")
        self.assertLess(time.perf_counter() - start, 0.5)

    def test_canonical_color_small_exponents(self):
        for number, expected in (("1e2", "100"), ("1e6", "1000000"), ("1e-6", "0.000001")):
            with self.subTest(number=number):
                self.assertEqual(check_design.canonical_color(f"rgb({number} 0 0)"), f"rgb({expected} 0 0)")

    def test_canonical_color_decimal_errors(self):
        for error in (check_design.InvalidOperation, ValueError):
            with self.subTest(error=error), mock.patch.object(check_design, "Decimal", side_effect=error):
                self.assertEqual(check_design.canonical_color("RGB(1.00E2 0 0)"), "rgb(1.00e2 0 0)")

    def test_black_white_colors(self):
        colors = ["#000", "#000000", "#fff", "#ffffff", "#0000", "#ffff"]
        colors.extend(f"#{base}{alpha:02x}" for base in ("000000", "ffffff") for alpha in range(256))
        for function, channels in (
            ("rgb", ("0 0 0", "255 255 255", "0% 0% 0%", "100% 100% 100%")),
            ("rgba", ("0 0 0", "255 255 255")),
            ("hsl", ("0 0% 0%", "0 0% 100%", "-720deg 0% 0%", "+123.5 0% 100%")),
            ("hsla", ("90 0% 0%", "240 0% 100%")),
            ("oklch", ("0 0 0", "1 0 0", "0 0 -42", "1 0 270deg")),
        ):
            for channel in channels:
                colors.extend((f"{function}({channel})", f"{function}({channel.replace(' ', ',')})"))
                for alpha in (".4", "40%", ".08", "0", "1"):
                    colors.extend((f"{function}({channel} / {alpha})", f"{function}({channel.replace(' ', ',')},{alpha})"))
        for color in colors:
            for tokens in (True, False):
                with self.subTest(color=color, tokens=tokens):
                    self.assertTrue(check_design.is_black_or_white(check_design.canonical_color(color)))
                    self.assertEqual(self.inspect(f".sample {{ color: {color}; }}", "raw-color", tokens=tokens), [])

    def test_near_black_white_colors_reported(self):
        for color in ("rgba(1,0,0,.5)", "#010101", "oklch(0.99 0 0)", "hsl(0 1% 50%)", "oklch(1 0.1 0)"):
            for tokens in (True, False):
                with self.subTest(color=color, tokens=tokens):
                    self.assertFalse(check_design.is_black_or_white(check_design.canonical_color(color)))
                    self.assertEqual(len(self.inspect(f".sample {{ color: {color}; }}", "raw-color", tokens=tokens)), 1)

    def test_hsl_lightness_extremes_any_saturation(self):
        for color, exempt in (
            ("hsl(120 50% 0%)", True), ("hsla(200, 80%, 100%, .5)", True),
            ("hsl(-30deg 60% 0% / 40%)", True), ("hsl(1turn 90% 100%)", True),
            ("hsl(2rad 40% 0%)", True), ("hsl(200grad 20% 100%)", True),
            ("hsl(0 0% 50%)", False), ("hsl(0% 50% 0%)", False), ("hsl(0 50 0%)", False),
        ):
            for tokens in (True, False):
                with self.subTest(color=color, tokens=tokens):
                    self.assertEqual(check_design.is_black_or_white(check_design.canonical_color(color)), exempt)
                    self.assertEqual(len(self.inspect(f".sample {{ color: {color}; }}", "raw-color", tokens=tokens)), 0 if exempt else 1)

    def test_oklch_black_any_chroma_white_zero_chroma(self):
        for color, exempt in (
            ("oklch(0 0.1 200)", True), ("oklch(0% 20% 90deg / .5)", True),
            ("oklch(1 0 200)", True), ("oklch(100% 0% 200)", True),
            ("oklch(1 0.05 200)", False), ("oklch(100% 5% 200)", False),
        ):
            for tokens in (True, False):
                with self.subTest(color=color, tokens=tokens):
                    self.assertEqual(check_design.is_black_or_white(check_design.canonical_color(color)), exempt)
                    self.assertEqual(len(self.inspect(f".sample {{ color: {color}; }}", "raw-color", tokens=tokens)), 0 if exempt else 1)

    def test_black_white_without_design_cli(self):
        path = self.write("sample.css", ".sample { color: #fff8; background: rgba(0,0,0,.4); }")
        with mock.patch.object(check_design, "design_path", return_value=None):
            code, out, err = self.invoke([str(path)], discovery=True)
        self.assertEqual((code, out), (0, "0 findings in 1 files\n"))
        self.assertIn("no DESIGN.md found", err)

    def test_black_white_predicate_invalid_components(self):
        for color in ("red", "rgb(0,0)", "rgb(0,0,0,1,2)", "rgb(0deg,0,0)", "hsl(none 0% 0%)", "oklch(1 0 none)", "rgba(0,0,0,1deg)"):
            with self.subTest(color=color):
                self.assertFalse(check_design.is_black_or_white(color))

    def test_raw_color_positive(self):
        for value in ("#def", "#def0", "#654321", "#654321ff", "rgb(1, 2, 3)", "rgba(1, 2, 3, .5)", "hsl(0 50% 50%)", "oklch(.5 .2 90)"):
            with self.subTest(value=value):
                self.assertEqual(len(self.inspect(f".sample {{ color: {value}; }}", "raw-color")), 1)

    def test_raw_color_negative(self):
        for value in ("#ABC", "#AaBbCc", "#123456", "var(--ink)", "#12345", "#123456789", "#abc_suffix"):
            with self.subTest(value=value):
                self.assertEqual(self.inspect(f".sample {{ color: {value}; }}", "raw-color"), [])

    def test_raw_color_token_blocks(self):
        content = ":root {\n  --ink: #def;\n  --wash: rgb(1, 2, 3);\n}\n@theme { --accent: #fed; }"
        self.assertEqual(self.inspect(content, "raw-color"), [])

    def test_raw_color_only_direct_custom_properties_are_exempt(self):
        content = ":root { color: #def; .nested { --ink: #fed; } }\n.sample { --ink: #def; }"
        self.assertEqual(self.inspect(content, "raw-color"), [(1, "raw-color", content.splitlines()[0])])

    def test_custom_properties_any_selector(self):
        for selector, color in ((".dark", "oklch(0.14 0 0)"), ("@theme inline", "#7c3aed"), (".card", "#def")):
            content = f"{selector} {{\n  --local: {color};\n  color: {color};\n}}"
            for suffix in (".css", ".html"):
                for tokens in (True, False):
                    with self.subTest(selector=selector, suffix=suffix, tokens=tokens):
                        source = f"<style>{content}</style>" if suffix == ".html" else content
                        self.assertEqual(self.inspect(source, "raw-color", suffix, tokens=tokens), [(3, "raw-color", f"color: {color};")])

    def test_raw_color_design_token_comment(self):
        self.assertEqual(self.inspect(".sample { color: #def; } /* design-token */", "raw-color"), [])
        self.assertTrue(self.inspect('.sample { content: "design-token"; color: #def; }', "raw-color"))

    def test_raw_color_without_design(self):
        self.assertTrue(self.inspect(".sample { color: #123456; }", "raw-color", tokens=False))

    def test_off_scale_spacing_positive(self):
        for name in ("margin", "padding", "gap", "top", "right", "bottom", "left", "inset", "width", "height", "margin-inline", "padding-top"):
            with self.subTest(name=name):
                self.assertTrue(self.inspect(f".sample {{ {name}: 13px; }}", "off-scale-spacing"))

    def test_off_scale_spacing_negative(self):
        content = ".sample { margin: 4px 8px 16px 24px; padding: 0px 1px 2px; width: 50%; font-size: 13px; }"
        self.assertEqual(self.inspect(content, "off-scale-spacing"), [])

    def test_off_scale_spacing_multiline(self):
        findings = self.inspect(".sample {\n  margin:\n    13px 16px;\n}", "off-scale-spacing")
        self.assertEqual(findings, [(2, "off-scale-spacing", "margin:")])

    def test_off_scale_spacing_tailwind_ignored(self):
        self.assertEqual(self.inspect('<div class="p-[13px]">Text</div>', "off-scale-spacing", ".tsx"), [])

    def test_banned_font_positive(self):
        for family in ("Inter", "Roboto", "Arial", "Open Sans", "Lato", "Helvetica Neue", "system-ui", "-apple-system", "Segoe UI"):
            with self.subTest(family=family):
                self.assertTrue(self.inspect(f'.sample {{ font-family: "{family}"; }}', "banned-font"))

    def test_banned_font_import_and_link(self):
        for content in ('@import "fonts?family=Open+Sans";', '<link href="fonts?family=Roboto" rel="stylesheet">'):
            with self.subTest(content=content):
                self.assertTrue(self.inspect(content, "banned-font", ".html"))

    def test_banned_font_negative(self):
        self.assertEqual(self.inspect('.sample { font-family: "Specimen Display", serif; }', "banned-font"), [])
        self.assertEqual(self.inspect(".sample { font-family: Interlude; }", "banned-font"), [])

    def test_design_family_always_allowed(self):
        self.tokens["families"] = {"inter", "open sans", "system-ui"}
        content = '.sample { font-family: Inter, "Open Sans", system-ui; }'
        self.assertEqual(self.inspect(content, "banned-font"), [])
        self.assertTrue(self.inspect(content, "banned-font", tokens=False))

    def test_missing_focus_visible_positive(self):
        for content in (".sample:focus { color: inherit; }", ".sample { outline: none; }", ".sample { outline: 0; }"):
            with self.subTest(content=content):
                self.assertEqual(len(self.inspect(content, "missing-focus-visible")), 1)

    def test_missing_focus_visible_negative(self):
        content = ".sample:focus { outline: none; }\n.sample:focus-visible { outline: 2px solid; }"
        self.assertEqual(self.inspect(content, "missing-focus-visible"), [])
        self.assertEqual(self.inspect(".sample { outline: 0.5px solid; }", "missing-focus-visible"), [])

    def test_focus_one_finding_on_first_line(self):
        findings = self.inspect(".sample {\noutline: none;\n}\na:focus { outline: 0; }", "missing-focus-visible")
        self.assertEqual([finding[0] for finding in findings], [2])

    def test_missing_reduced_motion_positive(self):
        for content in (".sample { animation: enter .2s; }", ".sample { transition: opacity .2s; }"):
            with self.subTest(content=content):
                self.assertEqual(len(self.inspect(content, "missing-reduced-motion")), 1)

    def test_missing_reduced_motion_negative(self):
        content = ".sample { transition: opacity .2s; }\n@media (prefers-reduced-motion: reduce) { .sample { transition: none; } }"
        self.assertEqual(self.inspect(content, "missing-reduced-motion"), [])
        self.assertEqual(self.inspect(".sample { transition: none; animation: none; }", "missing-reduced-motion"), [])
        self.assertEqual(self.inspect(".sample { transition: none !important; }", "missing-reduced-motion"), [])

    def test_motion_one_finding_on_first_line(self):
        content = ".sample {\ntransition: opacity .2s;\nanimation: enter .2s;\n}"
        self.assertEqual([finding[0] for finding in self.inspect(content, "missing-reduced-motion")], [2])

    def test_style_blocks(self):
        for suffix in (".html", ".vue", ".svelte", ".tsx", ".jsx"):
            with self.subTest(suffix=suffix):
                content = '<div>Text</div>\n<style>\n.sample:focus { transition: all .2s; }\n</style>'
                path = self.write("sample" + suffix, content)
                findings = check_design.inspect_file(path, {"missing-focus-visible", "missing-reduced-motion", "transition-all"}, self.tokens)
                self.assertEqual(len(findings), 3)
                self.assertTrue(all(finding[0] == 3 for finding in findings))

    def test_markup_text_is_not_css(self):
        content = '<div>:focus outline: none; transition: all .2s;</div>'
        for rule in ("missing-focus-visible", "missing-reduced-motion", "transition-all"):
            with self.subTest(rule=rule):
                self.assertEqual(self.inspect(content, rule, ".html"), [])

    def test_transition_all_positive(self):
        for property_name in ("transition", "transition-property"):
            with self.subTest(property_name=property_name):
                self.assertTrue(self.inspect(f".sample {{ {property_name}: all .2s; }}", "transition-all"))

    def test_transition_all_negative(self):
        self.assertEqual(self.inspect(".sample { transition: opacity .2s; transition-property: color; }", "transition-all"), [])

    def test_hover_scale_positive(self):
        self.assertTrue(self.inspect(".sample:hover {\n transform: scale(1.05);\n}", "hover-scale"))
        self.assertTrue(self.inspect('<div className="hover:scale-105">Text</div>', "hover-scale", ".tsx"))

    def test_hover_scale_negative(self):
        self.assertEqual(self.inspect(".sample { transform: scale(1.05); } .sample:hover { color: inherit; }", "hover-scale"), [])
        self.assertEqual(self.inspect('<div class="scale-105 hover:opacity-90">Text</div>', "hover-scale", ".html"), [])

    def test_emoji_icon_positive(self):
        for content in ('<span class="status-icon">\U0001f680</span>', '<span className="btn">\u2605</span>', '<div class="button"><span>\U0001f680</span></div>', '<button>\U0001f680 Go</button>', '<a href="#">\u2605 Go</a>'):
            with self.subTest(content=content):
                self.assertTrue(self.inspect(content, "emoji-icon", ".tsx"))

    def test_emoji_icon_negative(self):
        for content in ('<p>\U0001f680</p>', '<button>Go</button>', '<span class="icon">SVG</span>', '<button><span>\U0001f680</span></button>'):
            with self.subTest(content=content):
                self.assertEqual(self.inspect(content, "emoji-icon", ".html"), [])

    def test_emoji_icon_nested_markup(self):
        content = '<main><div><button>\U0001f680 Go</button></div></main>'
        self.assertEqual(len(self.inspect(content, "emoji-icon", ".vue")), 1)

    def test_gradient_text_positive(self):
        self.assertTrue(self.inspect(".sample { background: linear-gradient(red, blue);\nbackground-clip: text; }", "gradient-text"))
        self.assertTrue(self.inspect('<div class="bg-gradient-to-r bg-clip-text">Text</div>', "gradient-text", ".html"))
        self.assertTrue(self.inspect('<div class="bg-gradient-to-r" style="background-clip: text">Text</div>', "gradient-text", ".html"))

    def test_gradient_text_negative(self):
        self.assertEqual(self.inspect(".first { background: linear-gradient(red, blue); } .second { background-clip: text; }", "gradient-text"), [])
        self.assertEqual(self.inspect('<div class="bg-gradient-to-r">Text</div><p class="bg-clip-text">Text</p>', "gradient-text", ".html"), [])

    def test_numbered_markers_positive(self):
        content = '<div><span>01</span></div>\n<div><span>02</span></div>\n<div><span>03</span></div>'
        self.assertEqual(self.inspect(content, "numbered-markers", ".html")[0][0], 1)
        content = '<div class="uppercase tracking-wide"><b>1.</b><b>2.</b><b>3.</b></div>'
        self.assertEqual(len(self.inspect(content, "numbered-markers", ".tsx")), 1)

    def test_numbered_markers_negative(self):
        for content in ('<b>01</b><b>02</b>', '<b>03</b><b>02</b><b>01</b>', '<b>1.</b><b>2.</b><b>3.</b>', '<b>01</b>' + '\n' * 41 + '<b>02</b><b>03</b>'):
            with self.subTest(content=content):
                self.assertEqual(self.inspect(content, "numbered-markers", ".html"), [])

    def test_comments_do_not_trigger_rules(self):
        content = '/* :focus { outline: none; transition: all .2s; color: #def; } */'
        path = self.write("comments.css", content)
        self.assertEqual(check_design.inspect_file(path, set(check_design.RULES), self.tokens), [])

    def test_rule_selection_and_output(self):
        path = self.write("sample.css", ".sample { margin: 13px; transition: all .2s; }")
        code, out, err = self.invoke(["--rules", "transition-all", str(path)])
        self.assertEqual(code, 1)
        self.assertEqual(out, f"{path}:1: transition-all: .sample {{ margin: 13px; transition: all .2s; }}\n1 findings in 1 files\n")
        self.assertEqual(err, "")

    def test_exit_zero(self):
        path = self.write("sample.css", ".sample { margin: 16px; color: #abc; }")
        self.assertEqual(self.invoke([str(path)]), (0, "0 findings in 1 files\n", ""))

    def test_usage_exit_two(self):
        for arguments in ([], ["--rules", "unknown", "."], ["--unknown"]):
            with self.subTest(arguments=arguments):
                code, out, err = self.invoke(arguments, discovery=True)
                self.assertEqual(code, 2)
                self.assertIn("error:", err)

    def test_extension_filter(self):
        self.write("sample.css", ".sample { transition: all .2s; }")
        self.write("sample.html", '<button>\U0001f680</button>')
        self.write("sample.txt", ".sample { transition: all .2s; }")
        code, out, err = self.invoke(["--rules", "transition-all", "--ext", "TXT", str(self.base)])
        self.assertEqual(code, 1)
        self.assertIn("sample.txt:1: transition-all:", out)
        self.assertTrue(out.endswith("1 findings in 1 files\n"))

    def test_directory_skips_and_deduplicates(self):
        path = self.write("sample.css", ".sample { color: inherit; }")
        for directory in check_design.SKIPPED_DIRECTORIES:
            self.write(f"{directory}/bad.css", ".sample { transition: all .2s; }")
        self.assertEqual(list(check_design.source_files([self.base, path], (".css",))), [path])

    def test_no_design_warning_and_remaining_rules(self):
        path = self.write("sample.css", ".sample { margin: 13px; color: #def; font-family: Inter; transition: all .2s; }")
        with mock.patch.object(check_design, "design_path", return_value=None):
            code, out, err = self.invoke([str(path)], discovery=True)
        self.assertEqual(code, 1)
        self.assertEqual(err, "check-design: no DESIGN.md found, token rules skipped\n")
        self.assertNotIn("off-scale-spacing", out)
        for rule in ("raw-color", "banned-font", "transition-all", "missing-reduced-motion"):
            self.assertIn(f": {rule}:", out)

    def test_missing_yaml_exit_two(self):
        path = self.write("sample.css", ".sample { color: #def; }")
        with mock.patch.dict(sys.modules, {"yaml": None}):
            code, out, err = self.invoke([str(path)])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err, "check-design: PyYAML is needed for token rules; run: pip install pyyaml\n")

    def test_no_design_does_not_import_yaml(self):
        path = self.write("sample.css", ".sample { color: inherit; }")
        with mock.patch.dict(sys.modules, {"yaml": None}), mock.patch.object(check_design, "design_path", return_value=None):
            code, out, err = self.invoke([str(path)], discovery=True)
        self.assertEqual(code, 0)
        self.assertIn("token rules skipped", err)

    def test_malformed_front_matter(self):
        path = self.write("sample.css", ".sample { transition: all .2s; }")
        for content in ("no front matter", "---\ncolor: [\n---\n", "---\ncolor: {}", "---\n- list\n---\n", "---\nspacing: {scale: wrong}\n---\n"):
            with self.subTest(content=content):
                self.design.write_text(content, encoding="utf-8")
                code, out, err = self.invoke([str(path)])
                self.assertEqual(code, 1)
                self.assertEqual(out.count(": design-file:"), 1)
                self.assertIn(f"{self.design}:1: design-file:", out)
                self.assertTrue(out.endswith("1 findings in 1 files\n"))
                self.assertNotIn("transition-all", out)

    def test_design_discovery_local_before_git(self):
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.base)
        with mock.patch.object(check_design.subprocess, "run") as run:
            self.assertEqual(check_design.design_path(None, self.base), Path("DESIGN.md"))
        run.assert_not_called()

    def test_design_discovery_git_toplevel(self):
        nested = self.base / "nested"
        nested.mkdir()
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(nested)
        result = subprocess.CompletedProcess([], 0, str(self.base) + "\n", "")
        with mock.patch.object(check_design.subprocess, "run", return_value=result) as run:
            self.assertEqual(check_design.design_path(None, nested), self.design)
        self.assertEqual(run.call_args.args[0][2], str(nested))

    def test_design_discovery_none_and_explicit(self):
        nested = self.base / "nested"
        nested.mkdir()
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(nested)
        with mock.patch.object(check_design.subprocess, "run", side_effect=OSError):
            self.assertIsNone(check_design.design_path(None, nested))
        self.assertEqual(check_design.design_path(str(self.design), nested), self.design)

    def test_excerpt_limit(self):
        findings = self.inspect(".sample { color: #def; " + " " * 150 + "}", "raw-color")
        self.assertEqual(len(findings[0][2]), 120)

    def test_missing_path_exit_two_after_summary(self):
        missing = self.base / "absent.css"
        code, out, err = self.invoke([str(missing)])
        self.assertEqual(code, 2)
        self.assertEqual(out, "0 findings in 0 files\n")
        self.assertEqual(err, f"check-design: path not found: {missing}\n")

    def test_missing_paths_keep_scanning(self):
        bad = self.write("bad.css", ".sample { transition: all .2s; }")
        good = self.write("good.css", ".sample { color: inherit; }")
        missing = [self.base / "absent", self.base / "missing.css"]
        code, out, err = self.invoke(["--rules", "transition-all", str(missing[0]), str(bad), str(missing[1]), str(good)])
        self.assertEqual(code, 2)
        self.assertIn(f"{bad}:1: transition-all:", out)
        self.assertTrue(out.endswith("1 findings in 2 files\n"))
        for path in missing:
            self.assertIn(f"check-design: path not found: {path}\n", err)

    def test_discovered_missing_yaml_falls_back(self):
        path = self.write("sample.css", ".sample { margin: 13px; transition: all .2s; }")
        with mock.patch.dict(sys.modules, {"yaml": None}), mock.patch.object(check_design, "design_path", return_value=self.design):
            code, out, err = self.invoke([str(path)], discovery=True)
        self.assertEqual(code, 1)
        self.assertNotIn("off-scale-spacing", out)
        self.assertIn(": transition-all:", out)
        self.assertIn(": missing-reduced-motion:", out)
        self.assertEqual(err, "check-design: PyYAML missing, token rules skipped; run: pip install pyyaml\n")

    def test_discovered_missing_yaml_with_token_free_rules(self):
        path = self.write("sample.css", ".sample { transition: opacity .2s; }")
        with mock.patch.dict(sys.modules, {"yaml": None}), mock.patch.object(check_design, "design_path", return_value=self.design):
            code, out, err = self.invoke(["--rules", "transition-all", str(path)], discovery=True)
        self.assertEqual(code, 0)
        self.assertEqual(out, "0 findings in 1 files\n")
        self.assertEqual(err, "check-design: PyYAML missing, token rules skipped; run: pip install pyyaml\n")

    def test_explicit_missing_yaml_with_token_free_rules(self):
        path = self.write("sample.css", ".sample { transition: all .2s; }")
        with mock.patch.dict(sys.modules, {"yaml": None}):
            code, out, err = self.invoke(["--rules", "transition-all", str(path)])
        self.assertEqual(code, 1)
        self.assertIn(": transition-all:", out)
        self.assertEqual(err, "check-design: PyYAML missing, token rules skipped; run: pip install pyyaml\n")

    def test_banned_font_whole_family_names(self):
        for family in ("Roboto Mono", "Inter Tight", "Arial Narrow", "Segoe UI Variable"):
            with self.subTest(family=family):
                self.tokens["families"] = {family.casefold()}
                content = f'.sample {{ font-family: "{family}", sans-serif; }}'
                self.assertEqual(self.inspect(content, "banned-font"), [])
                self.assertEqual(self.inspect(content, "banned-font", tokens=False), [])

    def test_imported_font_whole_family_names(self):
        for content in ('@import "fonts?family=Roboto+Mono";', '<link href="fonts?family=Inter+Tight" rel="stylesheet">'):
            with self.subTest(content=content):
                self.assertEqual(self.inspect(content, "banned-font", ".html", tokens=False), [])

    def test_all_type_families_allowed(self):
        self.design.write_text("---\ntype:\n  mono: {family: Inter}\n  accent: {family: Roboto}\n  custom: {nested: {family: Arial}}\n---\n", encoding="utf-8")
        self.tokens = check_design.read_design(self.design)
        self.assertEqual(self.tokens["families"], {"inter", "roboto", "arial"})
        for family in ("Inter", "Roboto", "Arial"):
            with self.subTest(family=family):
                self.assertEqual(self.inspect(f".sample {{ font-family: {family}; }}", "banned-font"), [])

    def test_missing_or_empty_spacing_scale_skipped(self):
        for spacing in ("", "spacing: {}\n", "spacing: {scale: []}\n"):
            with self.subTest(spacing=spacing):
                self.design.write_text("---\n" + spacing + "color: {}\n---\n", encoding="utf-8")
                self.tokens = check_design.read_design(self.design)
                self.assertEqual(self.inspect(".sample { margin: 13px; }", "off-scale-spacing"), [])
                self.assertEqual(self.inspect('<div style="margin: 13px">Text</div>', "off-scale-spacing", ".html"), [])

    def test_negative_spacing_uses_magnitude(self):
        self.assertEqual(self.inspect(".sample { margin-top: -8px; padding: -1px -2px; left: -16px; }", "off-scale-spacing"), [])
        self.assertTrue(self.inspect(".sample { margin-top: -13px; }", "off-scale-spacing"))

    def test_row_and_column_gap(self):
        for property_name in ("row-gap", "column-gap"):
            with self.subTest(property_name=property_name):
                self.assertTrue(self.inspect(f".sample {{ {property_name}: 13px; }}", "off-scale-spacing"))
                self.assertEqual(self.inspect(f".sample {{ {property_name}: 8px; }}", "off-scale-spacing"), [])

    def test_inline_style_rules_report_attribute_line(self):
        content = '<div\n style="\n color: #def;\n margin: 13px;\n transition: all .2s;\n ">Text</div>'
        for suffix in check_design.MARKUP_EXTENSIONS:
            with self.subTest(suffix=suffix):
                path = self.write("sample" + suffix, content)
                findings = check_design.inspect_file(path, {"raw-color", "off-scale-spacing", "transition-all"}, self.tokens)
                self.assertEqual({finding[1] for finding in findings}, {"raw-color", "off-scale-spacing", "transition-all"})
                self.assertTrue(all(finding[0] == 2 and finding[2] == 'style="' for finding in findings))

    def test_inline_style_allowed_values(self):
        content = '<div style="color: #abc; margin: -8px 1px -2px; row-gap: 16px; transition: opacity .2s">Text</div>'
        for rule in ("raw-color", "off-scale-spacing", "transition-all"):
            with self.subTest(rule=rule):
                self.assertEqual(self.inspect(content, rule, ".html"), [])

    def test_inline_styles_after_jsx_callback(self):
        content = '<button onClick={() => go()} style="margin: 13px; transition: all .2s">Go</button>'
        for rule in ("off-scale-spacing", "transition-all"):
            with self.subTest(rule=rule):
                self.assertTrue(self.inspect(content, rule, ".tsx"))

    def test_emoji_nested_buttons_only_inner(self):
        content = '<button>Outer\n<button>\U0001f680 Inner</button>\n</button>'
        findings = self.inspect(content, "emoji-icon", ".tsx")
        self.assertEqual(findings, [(2, "emoji-icon", '<button>\U0001f680 Inner</button>')])

    def test_emoji_void_tags_performance(self):
        content = '<main>' + '<img src="image" alt="sample">' * 2000 + '<button>\U0001f680 Go</button></main>'
        start = time.perf_counter()
        findings = self.inspect(content, "emoji-icon", ".html")
        elapsed = time.perf_counter() - start
        self.assertEqual(len(findings), 1)
        self.assertLess(elapsed, 2)

    def test_emoji_void_and_self_closing_tags_ignored(self):
        content = '<div>' + ''.join(f'<{name} class="icon" title="\U0001f680">' for name in sorted(check_design.VOID_TAGS)) + '<span class="icon" title="\U0001f680"/><button>Go</button></div>'
        self.assertEqual(self.inspect(content, "emoji-icon", ".html"), [])

    def test_jsx_arrow_gradient_text(self):
        content = '<button onClick={() => go()} className="bg-clip-text bg-gradient-to-r">Go</button>'
        self.assertTrue(self.inspect(content, "gradient-text", ".tsx"))

    def test_jsx_arrow_hover_scale(self):
        content = '<button onClick={() => go()} className="bg-clip-text bg-gradient-to-r hover:scale-105">Go</button>'
        self.assertTrue(self.inspect(content, "hover-scale", ".tsx"))

    def test_jsx_tag_quotes_and_nested_braces(self):
        content = '<button title="a > b" onClick={() => go({label: "}>"})} className="hover:scale-105">Go</button>'
        self.assertTrue(self.inspect(content, "hover-scale", ".tsx"))

    def test_excerpt_line_separators(self):
        content = '.label { content: "\f\u2028\r"; }\n.sample { color: #def; }'
        self.assertEqual(self.inspect(content, "raw-color"), [(2, "raw-color", ".sample { color: #def; }")])

    def test_font_first_family_rule(self):
        for tokens in (True, False):
            with self.subTest(tokens=tokens):
                self.assertEqual(self.inspect('.sample { font-family: "Geist", system-ui, sans-serif; }', "banned-font", tokens=tokens), [])
                self.assertTrue(self.inspect('.sample { font-family: "iNtEr", sans-serif; }', "banned-font", tokens=tokens))

    def test_font_first_family_allowed_design(self):
        self.tokens["families"] = {"inter"}
        self.assertEqual(self.inspect('.sample { font-family: "Inter", Arial, sans-serif; }', "banned-font"), [])


if __name__ == "__main__":
    unittest.main()
