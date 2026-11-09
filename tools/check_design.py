import argparse
import os
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import unquote_plus

from check_writing import comma_list


DEFAULT_RULES = (
    "raw-color", "off-scale-spacing", "banned-font", "missing-focus-visible",
    "missing-reduced-motion", "transition-all", "hover-scale", "emoji-icon",
    "gradient-text", "numbered-markers",
)
RULES = DEFAULT_RULES
TOKEN_RULES = {"raw-color", "off-scale-spacing", "banned-font"}
DEFAULT_EXTENSIONS = (".css", ".scss", ".html", ".jsx", ".tsx", ".vue", ".svelte")
SKIPPED_DIRECTORIES = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next",
}
MARKUP_EXTENSIONS = {".html", ".jsx", ".tsx", ".vue", ".svelte"}
VOID_TAGS = {"img", "br", "hr", "input", "link", "meta", "source", "wbr", "area", "base", "col", "embed", "track"}
HEX_VALUE = re.compile(r"#[0-9a-fA-F]{3,8}\Z")
COLOR_LITERAL = re.compile(
    r"(?<![\w#])#(?:[0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{4}|[0-9a-f]{3})\b"
    r"|\b(?:rgba?|hsla?|oklch)\([^)]*\)", re.IGNORECASE,
)
COLOR_FUNCTION = re.compile(r"(rgba?|hsla?|oklch)\(([^)]*)\)\Z", re.IGNORECASE)
COLOR_NUMBER = re.compile(r"(?<![\w.])[+-]?(?:\d*\.\d+|\d+\.?\d*)(?:e[+-]?\d+)?")
COLOR_WHITESPACE = re.compile(r"\s+")
COLOR_SEPARATOR = re.compile(r"\s*([,/])\s*")
COLOR_COMPONENTS = re.compile(r"[,/ ]+")
COLOR_COMPONENT = re.compile(r"(-?(?:\d*\.\d+|\d+))(deg|grad|rad|turn|%)?\Z")
COMMENT = re.compile(r"/\*.*?\*/|<!--.*?-->", re.DOTALL)
COMMENT_PADDING = re.compile(r"[^\n]")
STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.IGNORECASE | re.DOTALL)
DECLARATION = re.compile(r"(?:^|[;{}])\s*([\w-]+)\s*:\s*([^;{}]+)", re.MULTILINE)
SPACING_PROPERTY = re.compile(
    r"(?:margin|padding|gap|row-gap|column-gap|inset|width|height|top|right|bottom|left)"
    r"(?:-(?:top|right|bottom|left|block|inline|start|end|row|column))*\Z",
)
PIXELS = re.compile(r"(?<![\w.])(-?(?:\d*\.)?\d+)px\b", re.IGNORECASE)
BANNED_FAMILIES = {"inter", "roboto", "arial", "open sans", "lato", "helvetica neue", "system-ui", "-apple-system", "segoe ui"}
FONT_IMPORT = re.compile(r"@import\b[^;]+", re.IGNORECASE)
FONT_QUERY = re.compile(r"[?&]family=([^&\"'();\s]+)", re.IGNORECASE)
QUOTED_VALUE = re.compile(r"([\"'])(.*?)\1", re.DOTALL)
IMPORTANT = re.compile(r"\s*!important\s*\Z", re.IGNORECASE)
FOCUS = re.compile(r":focus(?![\w-])|\boutline\s*:\s*(?:none|0)(?![\w.%])", re.IGNORECASE)
FOCUS_VISIBLE = re.compile(r":focus-visible\b", re.IGNORECASE)
MOTION = re.compile(r"(?:animation|transition)(?:-name|-property)?\Z", re.IGNORECASE)
NO_MOTION = re.compile(r"none\s*(?:!important)?\Z", re.IGNORECASE)
REDUCED_MOTION = re.compile(r"@media\s*\(\s*prefers-reduced-motion\b", re.IGNORECASE)
TRANSITION_ALL = re.compile(r"transition(?:-property)?\s*:\s*all\b", re.IGNORECASE)
BLOCK = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
HOVER = re.compile(r":hover\b", re.IGNORECASE)
SCALE = re.compile(r"transform\s*:\s*[^;{}]*\bscale\(", re.IGNORECASE)
HOVER_CLASS = re.compile(r"\bhover:scale-[\w.\[\]-]+")
EMOJI = re.compile(r"[\U0001f300-\U0001faff\u2600-\u27bf]")
CLASS = re.compile(r"\bclass(?:Name)?\s*=\s*(?:\{\s*)?([\"'])(.*?)\1", re.DOTALL)
STYLE_ATTRIBUTE = re.compile(r"(?<![\w:-])style\s*=\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL)
ICON_CLASS = re.compile(r"icon|btn|button", re.IGNORECASE)
GRADIENT = re.compile(r"(?:linear|radial|conic)-gradient\s*\(", re.IGNORECASE)
TEXT_CLIP = re.compile(r"(?:-webkit-)?background-clip\s*:\s*text\b", re.IGNORECASE)
TAG_START = re.compile(r"<(/?)([\w-]+)\b")
GRADIENT_CLASS = re.compile(r"\bbg-(?:gradient|linear|radial|conic)(?:-|\b)|gradient\(")
MARKER = re.compile(r">\s*(0[1-3]|[1-3]\.)\s*<")
TRACKED_LABEL = re.compile(r"uppercase|tracking-|text-transform\s*:\s*uppercase|letter-spacing", re.IGNORECASE)
CUSTOM_PROPERTY = re.compile(r"\s*[-]{2}[\w-]+\s*:")


def parse_args(arguments):
    parser = argparse.ArgumentParser()
    parser.add_argument("--design")
    parser.add_argument("--rules", type=comma_list, default=DEFAULT_RULES)
    parser.add_argument("--ext", type=comma_list, default=DEFAULT_EXTENSIONS)
    parser.add_argument("paths", nargs="+")
    options = parser.parse_args(arguments)
    unknown = [rule for rule in options.rules if rule not in RULES]
    if unknown:
        parser.error(f"unknown rule: {', '.join(unknown)}")
    options.ext = tuple(
        extension.lower() if extension.startswith(".") else "." + extension.lower()
        for extension in options.ext
    )
    return options


def source_files(paths, extensions, missing=None):
    seen = set()
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = []
            for root, directories, names in os.walk(path):
                directories[:] = sorted(
                    name for name in directories if name not in SKIPPED_DIRECTORIES
                )
                candidates.extend(Path(root) / name for name in sorted(names))
        else:
            print(f"check-design: path not found: {path}", file=sys.stderr)
            if missing is not None:
                missing.append(path)
            continue
        for candidate in candidates:
            if candidate.suffix.lower() not in extensions:
                continue
            key = os.path.normcase(str(candidate.resolve()))
            if key not in seen:
                seen.add(key)
                yield candidate


def design_path(explicit, first_path):
    if explicit is not None:
        return Path(explicit)
    local = Path("DESIGN.md")
    if local.is_file():
        return local
    first = Path(first_path).resolve()
    directory = first if first.is_dir() else first.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
    except OSError:
        return None
    if result.returncode == 0:
        candidate = Path(result.stdout.strip()) / "DESIGN.md"
        if candidate.is_file():
            return candidate
    return None


def normalize_hex(value):
    value = value.lower()
    if len(value) in (4, 5):
        return "#" + "".join(character * 2 for character in value[1:])
    return value


def canonical_color(value):
    value = value.strip().lower()
    if HEX_VALUE.fullmatch(value):
        return normalize_hex(value)
    match = COLOR_FUNCTION.fullmatch(value)
    if match is None:
        return value

    def normalize_number(match):
        text = match.group().lower()
        try:
            if "e" in text and abs(int(text.rsplit("e", 1)[1])) > 6:
                return text
            number = format(Decimal(text), "f")
        except (InvalidOperation, ValueError):
            return text
        return number.rstrip("0").rstrip(".") if "." in number else number

    arguments = COLOR_WHITESPACE.sub(" ", match.group(2).strip())
    arguments = COLOR_SEPARATOR.sub(r"\1", arguments)
    arguments = COLOR_NUMBER.sub(normalize_number, arguments)
    return f"{match.group(1)}({arguments})"


def is_black_or_white(canonical):
    if HEX_VALUE.fullmatch(canonical):
        return len(canonical) in (7, 9) and canonical[1:7] in {"000000", "ffffff"}
    match = COLOR_FUNCTION.fullmatch(canonical)
    if match is None:
        return False
    components = [COLOR_COMPONENT.fullmatch(part) for part in COLOR_COMPONENTS.split(match.group(2))]
    if len(components) not in (3, 4) or not all(components):
        return False
    values = [(Decimal(part.group(1)), part.group(2)) for part in components]
    if len(values) == 4 and values[3][1] not in (None, "%"):
        return False
    first, second, third = values[:3]
    if match.group(1) in {"rgb", "rgba"}:
        return all(unit in (None, "%") for _, unit in values[:3]) and (
            all(number == 0 for number, _ in values[:3])
            or all(number == (100 if unit == "%" else 255) for number, unit in values[:3])
        )
    if match.group(1) in {"hsl", "hsla"}:
        return first[1] in (None, "deg", "grad", "rad", "turn") and second[1] == "%" and third in {(0, "%"), (100, "%")}
    return second[1] in (None, "%") and third[1] in (None, "deg", "grad", "rad", "turn") and (
        first in {(0, None), (0, "%")}
        or first in {(1, None), (100, "%")} and second[0] == 0
    )


def font_families(value):
    return [part.strip().strip("\"'").casefold() for part in comma_list(IMPORTANT.sub("", value))]


def imported_families(source):
    queries = FONT_QUERY.findall(source)
    if queries:
        return [unquote_plus(family.split(":", 1)[0]).casefold() for query in queries for family in query.split("|")]
    return [family for match in QUOTED_VALUE.finditer(source) for family in font_families(match.group(2))]


def read_design(path):
    try:
        import yaml
    except ImportError:
        raise RuntimeError("check-design: PyYAML is needed for token rules; run: pip install pyyaml") from None
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("expected YAML front matter")
    try:
        closing = lines.index("---", 1)
    except ValueError:
        raise ValueError("unterminated YAML front matter") from None
    try:
        data = yaml.safe_load("\n".join(lines[1:closing]))
    except yaml.YAMLError:
        raise ValueError("invalid YAML front matter") from None
    if not isinstance(data, dict):
        raise ValueError("front matter must be a mapping")
    for key in ("color", "spacing", "radius", "type", "motion"):
        if not isinstance(data.get(key, {}), dict):
            raise ValueError(f"{key} must be a mapping")
    colors = {}

    def collect_colors(node, prefix=""):
        for key, value in node.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                collect_colors(value, name)
            elif isinstance(value, str):
                colors[name] = value

    collect_colors(data.get("color", {}))

    def resolve_color(name, seen):
        value = colors.get(name)
        if value is None or name in seen:
            return None
        if HEX_VALUE.fullmatch(value.strip()) or COLOR_LITERAL.fullmatch(value.strip()):
            return canonical_color(value)
        return resolve_color(value, seen | {name})

    typography = data.get("type", {})
    families = set()
    for role in ("display", "body"):
        definition = typography.get(role, {})
        if not isinstance(definition, dict):
            raise ValueError(f"type.{role} must be a mapping")

    def collect_families(node):
        family = node.get("family")
        if isinstance(family, str):
            families.update(font_families(family))
        for value in node.values():
            if isinstance(value, dict):
                collect_families(value)

    collect_families(typography)
    spacing = data.get("spacing", {}).get("scale", [])
    if not isinstance(spacing, list) or any(type(value) not in (int, float) for value in spacing):
        raise ValueError("spacing.scale must be a list of numbers")
    return {
        "colors": {value for name in colors if (value := resolve_color(name, set()))},
        "spacing": set(spacing),
        "radius": data.get("radius", {}),
        "type_scale": typography.get("scale", {}),
        "families": families,
        "icons": data.get("icons"),
        "reduced_motion": data.get("motion", {}).get("reduced_motion"),
    }


def excerpt(line):
    return line.strip()[:120]


def without_comments(text):
    return COMMENT.sub(lambda match: COMMENT_PADDING.sub(" ", match.group()), text)


def token_definitions(css):
    return [
        (declaration.start(1), declaration.end(2))
        for declaration in DECLARATION.finditer(css)
        if CUSTOM_PROPERTY.match(declaration.group(1) + ":")
    ]


def markup_tags(text):
    position = 0
    while (match := TAG_START.search(text, position)) is not None:
        index = match.end()
        quote = None
        depth = 0
        while index < len(text):
            character = text[index]
            if quote is not None:
                if character == "\\":
                    index += 2
                    continue
                if character == quote:
                    quote = None
            elif character in "\"'`":
                quote = character
            elif character == "{":
                depth += 1
            elif character == "}" and depth:
                depth -= 1
            elif character == ">" and not depth:
                break
            index += 1
        if index >= len(text):
            break
        yield (match.start(), index + 1, match.group(2).lower(), bool(match.group(1)), text[match.end():index])
        position = index + 1


def emoji_elements(text, tags):
    stack = []
    boundary = 0
    for start, end, name, closing, attributes in tags:
        if stack and EMOJI.search(text[boundary:start]):
            stack[-1]["direct"] = True
            stack[-1]["emoji"] = True
        boundary = end
        if name in VOID_TAGS or attributes.rstrip().endswith("/"):
            continue
        if not closing:
            stack.append({"name": name, "start": start, "attributes": attributes, "direct": False, "emoji": False})
            continue
        for index in range(len(stack) - 1, -1, -1):
            if stack[index]["name"] == name:
                break
        else:
            continue
        while len(stack) > index:
            node = stack.pop()
            if stack:
                stack[-1]["emoji"] |= node["emoji"]
            if node["name"] == name:
                yield node


def inspect_file(path, enabled, tokens=None):
    with path.open(encoding="utf-8", errors="replace", newline="") as source:
        text = source.read()
    lines = text.split("\n")
    clean = without_comments(text)
    markup = path.suffix.lower() in MARKUP_EXTENSIONS
    sections = [(match.start(1), match.group(1)) for match in STYLE_BLOCK.finditer(clean)] if markup else [(0, clean)]
    tags = list(markup_tags(clean)) if markup else []
    inline_styles = [
        (start + match.start(), start + match.start(2), match.group(2))
        for start, end, name, closing, attributes in tags if not closing
        for match in STYLE_ATTRIBUTE.finditer(clean[start:end])
    ]
    findings = []

    def report(position, rule):
        line = text.count("\n", 0, position) + 1
        finding = (line, rule, excerpt(lines[line - 1]))
        if finding not in findings:
            findings.append(finding)

    if "raw-color" in enabled:
        comment_lines = {
            text.count("\n", 0, comment.start()) + index
            for comment in COMMENT.finditer(text)
            for index, line in enumerate(comment.group().split("\n"))
            if "design-token" in line
        }
        definitions = [
            (offset + start, offset + end)
            for offset, css in sections for start, end in token_definitions(css)
        ]
        for match in COLOR_LITERAL.finditer(clean):
            line = text.count("\n", 0, match.start())
            if line in comment_lines or any(start <= match.start() < end for start, end in definitions):
                continue
            color = canonical_color(match.group())
            if is_black_or_white(color):
                continue
            if tokens is None or color not in tokens["colors"]:
                position = next((attribute for attribute, offset, value in inline_styles if offset <= match.start() < offset + len(value)), match.start())
                report(position, "raw-color")

    if "banned-font" in enabled:
        allowed = tokens["families"] if tokens is not None else set()
        for offset, css in sections + [(attribute, value) for attribute, _, value in inline_styles]:
            for declaration in DECLARATION.finditer(css):
                if declaration.group(1).lower() == "font-family":
                    families = font_families(declaration.group(2))
                    if families and families[0] in BANNED_FAMILIES - allowed:
                        report(offset + declaration.start(1), "banned-font")
        sources = [(match.start(), match.group()) for match in FONT_IMPORT.finditer(clean)]
        sources.extend((start, clean[start:end]) for start, end, name, closing, _ in tags if name == "link" and not closing)
        for position, source in sources:
            if any(family in BANNED_FAMILIES - allowed for family in imported_families(source)):
                report(position, "banned-font")

    css_text = "\n".join(css for _, css in sections)
    focus_visible = FOCUS_VISIBLE.search(css_text)
    reduced_motion = REDUCED_MOTION.search(css_text)
    focus_position = None
    motion_position = None
    for offset, css in sections:
        if "missing-focus-visible" in enabled and not focus_visible and focus_position is None:
            match = FOCUS.search(css)
            if match:
                focus_position = offset + match.start()
        for declaration in DECLARATION.finditer(css):
            property_name, value = declaration.group(1).lower(), declaration.group(2).strip()
            if "off-scale-spacing" in enabled and tokens is not None and tokens["spacing"] and SPACING_PROPERTY.fullmatch(property_name):
                for pixels in PIXELS.finditer(value):
                    number = abs(float(pixels.group(1)))
                    if number not in tokens["spacing"] and number not in {0, 1, 2}:
                        report(offset + declaration.start(1), "off-scale-spacing")
            if "missing-reduced-motion" in enabled and not reduced_motion and motion_position is None:
                if MOTION.fullmatch(property_name) and not NO_MOTION.fullmatch(value):
                    motion_position = offset + declaration.start(1)
        if "transition-all" in enabled:
            for match in TRANSITION_ALL.finditer(css):
                report(offset + match.start(), "transition-all")
        for block in BLOCK.finditer(css):
            selector, body = block.group(1), block.group(2)
            if "hover-scale" in enabled and HOVER.search(selector) and SCALE.search(body):
                report(offset + block.start(1) + HOVER.search(selector).start(), "hover-scale")
            if "gradient-text" in enabled and TEXT_CLIP.search(body) and GRADIENT.search(body):
                report(offset + block.start(2) + TEXT_CLIP.search(body).start(), "gradient-text")
    for attribute, offset, css in inline_styles:
        if "off-scale-spacing" in enabled and tokens is not None and tokens["spacing"]:
            for declaration in DECLARATION.finditer(css):
                if SPACING_PROPERTY.fullmatch(declaration.group(1).lower()):
                    if any(abs(float(pixels.group(1))) not in tokens["spacing"] | {0, 1, 2} for pixels in PIXELS.finditer(declaration.group(2))):
                        report(attribute, "off-scale-spacing")
        if "transition-all" in enabled and TRANSITION_ALL.search(css):
            report(attribute, "transition-all")
    if focus_position is not None:
        report(focus_position, "missing-focus-visible")
    if motion_position is not None:
        report(motion_position, "missing-reduced-motion")

    if markup:
        if "hover-scale" in enabled:
            for start, end, _, closing, _ in tags:
                if not closing:
                    for match in HOVER_CLASS.finditer(clean[start:end]):
                        report(start + match.start(), "hover-scale")
        if "emoji-icon" in enabled:
            for element in emoji_elements(clean, tags):
                classes = CLASS.search(element["attributes"])
                icon = classes is not None and ICON_CLASS.search(classes.group(2))
                if (icon and element["emoji"]) or (element["name"] in {"button", "a"} and element["direct"]):
                    report(element["start"], "emoji-icon")
        if "gradient-text" in enabled:
            for start, end, _, closing, _ in tags:
                if closing:
                    continue
                value = clean[start:end]
                clipped = "bg-clip-text" in value or TEXT_CLIP.search(value)
                gradient = GRADIENT_CLASS.search(value) or GRADIENT.search(value)
                if clipped and gradient:
                    report(start, "gradient-text")
        if "numbered-markers" in enabled:
            markers = list(MARKER.finditer(clean))
            for first, second, third in zip(markers, markers[1:], markers[2:]):
                values = [match.group(1) for match in (first, second, third)]
                if values not in [["01", "02", "03"], ["1.", "2.", "3."]]:
                    continue
                if text.count("\n", first.start(), third.end()) > 40:
                    continue
                if values[0] == "1." and not TRACKED_LABEL.search(clean[max(0, first.start() - 200):third.end()]):
                    continue
                report(first.start(), "numbered-markers")
                break
    findings.sort(key=lambda finding: finding[0])
    return findings


def main(arguments=None):
    options = parse_args(sys.argv[1:] if arguments is None else arguments)
    path = design_path(options.design, options.paths[0])
    tokens = None
    if path is None:
        print("check-design: no DESIGN.md found, token rules skipped", file=sys.stderr)
    else:
        try:
            tokens = read_design(path)
        except RuntimeError as exc:
            if options.design is not None and TOKEN_RULES.intersection(options.rules):
                print(str(exc), file=sys.stderr)
                return 2
            print("check-design: PyYAML missing, token rules skipped; run: pip install pyyaml", file=sys.stderr)
        except (ValueError, OSError) as exc:
            print(f"{path}:1: design-file: {excerpt(str(exc))}")
            print("1 findings in 1 files")
            return 1
    findings = 0
    files = 0
    missing = []
    try:
        for path in source_files(options.paths, options.ext, missing):
            files += 1
            for line_number, rule, text in inspect_file(path, set(options.rules), tokens):
                findings += 1
                print(f"{path}:{line_number}: {rule}: {text}")
    except OSError as exc:
        print(f"check-design: {exc}", file=sys.stderr)
        return 2
    print(f"{findings} findings in {files} files")
    if missing:
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
