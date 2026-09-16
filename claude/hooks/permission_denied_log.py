import sys

from _common import append_jsonl, hooks_home, read_payload, utc_timestamp


def truncate_strings(value):
    if isinstance(value, str):
        return (value[:500], len(value) > 500)
    if isinstance(value, list):
        result = []
        truncated = False
        for item in value:
            clean, item_truncated = truncate_strings(item)
            result.append(clean)
            truncated = truncated or item_truncated
        return result, truncated
    if isinstance(value, dict):
        result = {}
        truncated = False
        for key, item in value.items():
            clean, item_truncated = truncate_strings(item)
            result[key] = clean
            truncated = truncated or item_truncated
        return result, truncated
    return value, False


def main():
    payload = read_payload()
    if payload is None:
        return 0
    try:
        tool_input = payload.get("tool_input", {})
        if not isinstance(tool_input, dict):
            tool_input = {}
        clean_input, truncated = truncate_strings(tool_input)
        if truncated:
            clean_input["truncated"] = True
        append_jsonl(
            hooks_home() / ".claude" / "denials.jsonl",
            {
                "ts": utc_timestamp(),
                "tool_name": payload.get("tool_name"),
                "reason": payload.get("reason"),
                "cwd": payload.get("cwd"),
                "tool_input": clean_input,
            },
        )
    except Exception as exc:
        sys.stderr.write(f"permission-denied-log: unable to append denial: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
