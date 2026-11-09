"""Render a URL or HTML file to PNGs or a PDF for design review."""

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


PLAYWRIGHT_INSTALL_HINT = "render_design: Playwright is not installed; run: pip install playwright && playwright install chromium"


def positive_integer(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer: {value}")
    if number <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def parse_widths(value):
    return [positive_integer(part.strip()) for part in value.split(",")]


def target_stem(target, dark=False, reduced_motion=False, name=None):
    if name is not None:
        stem = name
    else:
        parsed = urlsplit(target)
        if parsed.scheme.lower() in ("http", "https"):
            stem = re.sub(r"[^A-Za-z0-9]+", "-", parsed.netloc + parsed.path).strip("-")[:60].rstrip("-")
        else:
            stem = Path(target).stem
    return stem + ("-dark" if dark else "") + ("-rm" if reduced_motion else "")


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target")
    parser.add_argument("--out", type=Path, default=Path(".scratch/render"))
    parser.add_argument("--widths", type=parse_widths, default="390,1280,1920")
    parser.add_argument("--height", type=positive_integer, default=900)
    parser.add_argument("--reduced-motion", action="store_true")
    parser.add_argument("--dark", action="store_true")
    parser.add_argument("--wait", type=int, default=300)
    parser.add_argument("--wait-until", choices=("networkidle", "load", "domcontentloaded"), default="networkidle")
    parser.add_argument("--timeout", type=int, default=30000)
    parser.add_argument("--selector")
    parser.add_argument("--pdf", action="store_true")
    parser.add_argument("--name")
    args = parser.parse_args(argv)
    if args.pdf and args.selector is not None:
        parser.error("--pdf cannot be combined with --selector")
    if args.wait < 0:
        parser.error("wait must be non-negative")
    if args.timeout < 0:
        parser.error("timeout must be non-negative")
    if args.name is not None and (not args.name or args.name in (".", "..") or "/" in args.name or "\\" in args.name):
        parser.error("name must be a file stem")
    return args


def main(argv=None):
    args = parse_args(argv)
    target = args.target
    if urlsplit(target).scheme.lower() not in ("http", "https"):
        path = Path(target).expanduser().resolve()
        if not path.is_file():
            print(f"render_design: no such file: {path}", file=sys.stderr)
            return 1
        target = path.as_uri()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(PLAYWRIGHT_INSTALL_HINT, file=sys.stderr)
        return 2
    stem = target_stem(args.target, args.dark, args.reduced_motion, name=args.name)
    try:
        output = args.out.expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                if args.pdf:
                    page = browser.new_page(viewport={"width": 1280, "height": args.height}, device_scale_factor=1,
                                            reduced_motion="reduce" if args.reduced_motion else "no-preference",
                                            color_scheme="dark" if args.dark else "light")
                    try:
                        page.goto(target, wait_until=args.wait_until, timeout=args.timeout)
                        page.wait_for_timeout(args.wait)
                        destination = output / f"{stem}.pdf"
                        page.pdf(path=str(destination), format="A4", print_background=True, prefer_css_page_size=True)
                        print(destination, flush=True)
                    finally:
                        page.close()
                    return 0
                selector_count = None
                for width in args.widths:
                    page = browser.new_page(viewport={"width": width, "height": args.height}, device_scale_factor=1,
                                            reduced_motion="reduce" if args.reduced_motion else "no-preference",
                                            color_scheme="dark" if args.dark else "light")
                    try:
                        page.goto(target, wait_until=args.wait_until, timeout=args.timeout)
                        page.wait_for_timeout(args.wait)
                        destination = output / f"{stem}-{width}.png"
                        if args.selector:
                            locator = page.locator(args.selector)
                            if selector_count is None:
                                selector_count = locator.count()
                                if selector_count == 0:
                                    print(f"render_design: selector matched nothing: {args.selector}", file=sys.stderr)
                                    return 1
                                if selector_count > 1:
                                    print(f"render_design: selector matched {selector_count} elements, using the first", file=sys.stderr)
                            locator.first.screenshot(path=str(destination))
                        else:
                            page.screenshot(path=str(destination), full_page=True)
                        print(destination, flush=True)
                    finally:
                        page.close()
            finally:
                browser.close()
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        print(message, file=sys.stderr)
        if "Executable doesn't exist" in message or "playwright install" in message:
            print(PLAYWRIGHT_INSTALL_HINT, file=sys.stderr)
            return 2
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
