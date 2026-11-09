import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import render_design


HTML = """<!doctype html>
<html><head><style>
body { margin: 32px; }
p { color: red; }
@media (prefers-reduced-motion: reduce) {
    h1::after { content: " Reduced motion"; }
}
</style></head><body><h1>Design review</h1><p>Render fixture.</p></body></html>
"""


class RenderDesignTest(unittest.TestCase):
    def test_file_stem(self):
        self.assertEqual(render_design.target_stem(str(Path("pages") / "sample.html")), "sample")

    def test_url_slug(self):
        self.assertEqual(render_design.target_stem("https://example.test/design/page.html?query=ignored#section"),
                         "example-test-design-page-html")

    def test_url_stem_trimmed_to_sixty_characters(self):
        self.assertEqual(render_design.target_stem("https://example.test/" + "a" * 100), "example-test-" + "a" * 47)

    def test_dark_and_reduced_motion_suffixes(self):
        for dark, reduced_motion, suffix in ((True, False, "-dark"), (False, True, "-rm"), (True, True, "-dark-rm")):
            with self.subTest(suffix=suffix):
                self.assertEqual(render_design.target_stem("sample.html", dark, reduced_motion), "sample" + suffix)
                self.assertEqual(render_design.target_stem("sample.html", dark, reduced_motion, name="custom.name"), "custom.name" + suffix)

    def test_default_widths(self):
        self.assertEqual(render_design.parse_args(["sample.html"]).widths, [390, 1280, 1920])

    def test_custom_widths(self):
        self.assertEqual(render_design.parse_args(["sample.html", "--widths", "400, 800"]).widths, [400, 800])

    def test_invalid_widths_exit_two(self):
        for widths in ("390,wide", "", "0", "-1"):
            with self.subTest(widths=widths), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                render_design.parse_args(["sample.html", "--widths", widths])
            self.assertEqual(error.exception.code, 2)

    def test_navigation_options(self):
        defaults = render_design.parse_args(["sample.html"])
        self.assertEqual((defaults.wait_until, defaults.timeout), ("networkidle", 30000))
        for wait_until in ("networkidle", "load", "domcontentloaded"):
            with self.subTest(wait_until=wait_until):
                args = render_design.parse_args(["sample.html", "--wait-until", wait_until, "--timeout", "1200"])
                self.assertEqual((args.wait_until, args.timeout), (wait_until, 1200))
        for options in (["--wait-until", "unknown"], ["--timeout", "-1"], ["--timeout", "many"]):
            with self.subTest(options=options), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                render_design.parse_args(["sample.html", *options])
            self.assertEqual(error.exception.code, 2)

    def test_pdf_selector_rejected(self):
        command = [sys.executable, str(Path(render_design.__file__).resolve()), "sample.html", "--pdf", "--selector", "x"]
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--pdf cannot be combined with --selector", result.stderr)

    def test_pdf_options_and_single_page(self):
        with tempfile.TemporaryDirectory() as folder:
            factory = mock.MagicMock()
            browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
            page = browser.new_page.return_value
            module = mock.Mock(sync_playwright=factory)
            with mock.patch.dict(sys.modules, {"playwright.sync_api": module}), contextlib.redirect_stdout(io.StringIO()) as output:
                result = render_design.main(["https://example.test/page", "--out", folder, "--pdf", "--widths", "390,1920",
                                             "--height", "1200", "--dark", "--reduced-motion", "--name", "brief", "--wait", "15",
                                             "--wait-until", "domcontentloaded", "--timeout", "1200"])
            destination = str(Path(folder).resolve() / "brief-dark-rm.pdf")
            self.assertEqual(result, 0)
            self.assertEqual(output.getvalue(), destination + "\n")
            browser.new_page.assert_called_once_with(viewport={"width": 1280, "height": 1200}, device_scale_factor=1,
                                                     reduced_motion="reduce", color_scheme="dark")
            page.goto.assert_called_once_with("https://example.test/page", wait_until="domcontentloaded", timeout=1200)
            page.wait_for_timeout.assert_called_once_with(15)
            page.pdf.assert_called_once_with(path=destination, format="A4", print_background=True, prefer_css_page_size=True)
            page.screenshot.assert_not_called()
            page.emulate_media.assert_not_called()
            page.close.assert_called_once_with()
            browser.close.assert_called_once_with()

    def test_pdf_error_handling(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "sample.html"
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(render_design.main([str(target), "--pdf"]), 1)
            self.assertIn("no such file:", error.getvalue())
            target.write_text(HTML, encoding="utf-8")
            with mock.patch.dict(sys.modules, {"playwright.sync_api": None}), contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(render_design.main([str(target), "--pdf"]), 2)
            self.assertEqual(error.getvalue(), render_design.PLAYWRIGHT_INSTALL_HINT + "\n")
            factory = mock.MagicMock()
            browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
            page = browser.new_page.return_value
            page.pdf.side_effect = RuntimeError("PDF output failed")
            module = mock.Mock(sync_playwright=factory)
            with mock.patch.dict(sys.modules, {"playwright.sync_api": module}), contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(render_design.main([str(target), "--pdf", "--out", folder]), 1)
            self.assertEqual(error.getvalue(), "PDF output failed\n")
            page.close.assert_called_once_with()
            browser.close.assert_called_once_with()

    def test_selector_matches_checked_once_before_screenshots(self):
        for count in (0, 1, 2):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as folder:
                factory = mock.MagicMock()
                browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
                page = browser.new_page.return_value
                locator = page.locator.return_value
                locator.count.return_value = count
                module = mock.Mock(sync_playwright=factory)
                with mock.patch.dict(sys.modules, {"playwright.sync_api": module}), \
                        contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
                    result = render_design.main(["https://example.test/page", "--out", folder, "--widths", "390,1280",
                                                 "--selector", "h1", "--name", "custom.name", "--dark", "--reduced-motion",
                                                 "--wait-until", "domcontentloaded", "--timeout", "1200"])
                locator.count.assert_called_once_with()
                if count == 0:
                    self.assertEqual(result, 1)
                    self.assertEqual(output.getvalue(), "")
                    self.assertEqual(error.getvalue(), "render_design: selector matched nothing: h1\n")
                    self.assertEqual(browser.new_page.call_count, 1)
                    locator.first.screenshot.assert_not_called()
                else:
                    self.assertEqual(result, 0)
                    expected = [str(Path(folder).resolve() / f"custom.name-dark-rm-{width}.png") for width in (390, 1280)]
                    self.assertEqual(output.getvalue().splitlines(), expected)
                    self.assertEqual(locator.first.screenshot.call_args_list, [mock.call(path=path) for path in expected])
                    self.assertEqual(error.getvalue(), "render_design: selector matched 2 elements, using the first\n" if count == 2 else "")
                self.assertTrue(all(call == mock.call("https://example.test/page", wait_until="domcontentloaded", timeout=1200)
                                    for call in page.goto.call_args_list))
                page.close.assert_called()
                browser.close.assert_called_once_with()

    def test_missing_browser_exits_two_with_full_error_and_install_hint(self):
        for message in ("Launch failed\nExecutable doesn't exist at browser path", "Launch failed\nRun playwright install chromium"):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as folder:
                module = mock.Mock(sync_playwright=mock.Mock(side_effect=RuntimeError(message)))
                with mock.patch.dict(sys.modules, {"playwright.sync_api": module}), contextlib.redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(render_design.main(["https://example.test", "--out", folder]), 2)
                self.assertEqual(error.getvalue(), message + "\n" + render_design.PLAYWRIGHT_INSTALL_HINT + "\n")

    def test_other_browser_errors_keep_full_message(self):
        message = "Navigation failed\nActionable detail"
        with tempfile.TemporaryDirectory() as folder:
            module = mock.Mock(sync_playwright=mock.Mock(side_effect=RuntimeError(message)))
            with mock.patch.dict(sys.modules, {"playwright.sync_api": module}), contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(render_design.main(["https://example.test", "--out", folder]), 1)
        self.assertEqual(error.getvalue(), message + "\n")

    def test_missing_file_exits_one(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stderr(io.StringIO()) as error:
            missing = Path(folder) / "missing.html"
            self.assertEqual(render_design.main([str(missing)]), 1)
        self.assertEqual(error.getvalue(), f"render_design: no such file: {missing.resolve()}\n")

    def test_missing_playwright_exits_two(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "sample.html"
            target.write_text(HTML, encoding="utf-8")
            with mock.patch.dict(sys.modules, {"playwright.sync_api": None}), contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(render_design.main([str(target)]), 2)
        self.assertEqual(error.getvalue(), "render_design: Playwright is not installed; run: pip install playwright && playwright install chromium\n")

    def test_render_html_and_reduced_motion(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            self.skipTest(str(exc))
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                self.skipTest(str(exc))
            browser.close()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "sample.html"
            target.write_text(HTML, encoding="utf-8")
            output = Path(folder) / "render"
            command = [sys.executable, str(Path(render_design.__file__).resolve()), str(target), "--out", str(output)]
            plain = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
            self.assertEqual(plain.returncode, 0, plain.stderr)
            print(plain.stdout, end="", flush=True)
            paths = [output / f"sample-{width}.png" for width in (390, 1280, 1920)]
            self.assertEqual(plain.stdout.splitlines(), [str(path.resolve()) for path in paths])
            for path in paths:
                self.assertGreater(path.stat().st_size, 0)
            reduced = subprocess.run(command + ["--reduced-motion"], capture_output=True, text=True, check=False, timeout=120)
            self.assertEqual(reduced.returncode, 0, reduced.stderr)
            for path in paths:
                variant = path.with_name(path.name.replace("sample-", "sample-rm-", 1))
                self.assertGreater(variant.stat().st_size, 0)
                self.assertNotEqual(path.read_bytes(), variant.read_bytes())

    def test_render_html_pdf(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            self.skipTest(str(exc))
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                self.skipTest(str(exc))
            browser.close()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "sample.html"
            target.write_text(HTML, encoding="utf-8")
            output = Path(folder) / "render"
            command = [sys.executable, str(Path(render_design.__file__).resolve()), str(target), "--pdf", "--out", str(output)]
            result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            paths = list(output.iterdir())
            self.assertEqual(paths, [output / "sample.pdf"])
            self.assertEqual(result.stdout.splitlines(), [str(paths[0].resolve())])
            self.assertEqual(paths[0].read_bytes()[:4], b"%PDF")
            self.assertGreater(paths[0].stat().st_size, 1000)
            named = subprocess.run(command + ["--name", "brief"], capture_output=True, text=True, check=False, timeout=120)
            self.assertEqual(named.returncode, 0, named.stderr)
            destination = output / "brief.pdf"
            self.assertEqual(named.stdout.splitlines(), [str(destination.resolve())])
            self.assertEqual(destination.read_bytes()[:4], b"%PDF")
            self.assertGreater(destination.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
