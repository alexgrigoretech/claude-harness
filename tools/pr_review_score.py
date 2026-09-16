import argparse
import datetime
import json
import sys
from pathlib import Path

if __package__:
    from . import pr_review_runner as runner
else:
    import pr_review_runner as runner


REPO_ROOT = getattr(runner, "REPO_ROOT", Path(__file__).resolve().parents[1])
# Agy and Codex run on subscription logins with zero marginal cost. List prices for
# a per-token comparison are recorded in the pr-review plan, not here.
DEFAULT_PRICES = {
    "agy": (0.0, 0.0),
    "codex": (0.0, 0.0),
    "kimi": (0.60, 2.50),
}


def parse_price(value):
    if "=" not in value:
        raise argparse.ArgumentTypeError("price must be name=in,out")
    name, amounts = value.split("=", 1)
    parts = amounts.split(",")
    if not name.strip() or len(parts) != 2:
        raise argparse.ArgumentTypeError("price must be name=in,out")
    try:
        input_price, output_price = (float(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("prices must be numbers") from exc
    if input_price < 0 or output_price < 0:
        raise argparse.ArgumentTypeError("prices must not be negative")
    return name.strip(), (input_price, output_price)


def load_expected(path):
    with Path(path).open(encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, list):
        raise ValueError("expected.json must contain a list")
    return value


def same_file(left, right):
    return str(left).replace("\\", "/").lstrip("./") == str(right).replace("\\", "/").lstrip("./")


def finding_matches(finding, defect):
    lines = defect.get("lines")
    line = finding.get("line")
    if (
        same_file(finding.get("file", ""), defect.get("file", ""))
        and isinstance(line, int)
        and isinstance(lines, list)
        and len(lines) == 2
        and all(isinstance(item, int) for item in lines)
        and lines[0] - 5 <= line <= lines[1] + 5
    ):
        return True
    text = f"{finding.get('claim', '')}\n{finding.get('evidence', '')}".lower()
    keywords = defect.get("keywords", [])
    return isinstance(keywords, list) and any(
        isinstance(keyword, str) and keyword.lower() in text for keyword in keywords if keyword
    )


def score_findings(findings, expected):
    matched = {
        index
        for index, defect in enumerate(expected)
        if isinstance(defect, dict) and any(finding_matches(finding, defect) for finding in findings)
    }
    false_positives = sum(
        1
        for finding in findings
        if not any(isinstance(defect, dict) and finding_matches(finding, defect) for defect in expected)
    )
    recall = len(matched) / len(expected) if expected else 0.0
    return recall, false_positives, len(matched)


def review_cost(usage, price):
    if price == (0.0, 0.0):
        return 0.0
    if usage is None or price is None:
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def score_cases(cases_dir, configs, prices, repo=None):
    repo = Path(repo if repo is not None else REPO_ROOT).resolve()
    rows = []
    case_paths = sorted(
        path for path in Path(cases_dir).iterdir() if path.is_dir() and (path / "diff.patch").is_file()
    )
    for case_path in case_paths:
        expected = load_expected(case_path / "expected.json")
        diff_text = (case_path / "diff.patch").read_text(encoding="utf-8-sig")
        body_path = case_path / "body.md"
        body = body_path.read_text(encoding="utf-8-sig") if body_path.is_file() else ""
        limited_diff, _ = runner.truncate_diff(diff_text, 400000)
        prompt = runner.build_prompt(body, limited_diff)
        for run in runner.run_reviewers(prompt, configs, repo=repo):
            recall, false_positives, matched = score_findings(run.findings, expected)
            rows.append(
                {
                    "case": case_path.name,
                    "reviewer": run.name,
                    "status": run.status,
                    "recall": recall,
                    "matched_defects": matched,
                    "expected_defects": len(expected),
                    "false_positives": false_positives,
                    "seconds": run.seconds,
                    "cost": review_cost(run.usage, prices.get(run.name)),
                    **({"usage": run.usage} if run.usage is not None else {}),
                    **({"error": run.error} if run.error is not None else {}),
                }
            )
    return rows


def reviewer_totals(rows):
    totals = []
    for name in dict.fromkeys(row["reviewer"] for row in rows):
        selected = [row for row in rows if row["reviewer"] == name]
        expected = sum(row["expected_defects"] for row in selected)
        matched = sum(row["matched_defects"] for row in selected)
        costs = [row["cost"] for row in selected if row["cost"] is not None]
        totals.append(
            {
                "reviewer": name,
                "reviews": len(selected),
                "recall": matched / expected if expected else 0.0,
                "matched_defects": matched,
                "expected_defects": expected,
                "false_positives": sum(row["false_positives"] for row in selected),
                "seconds": round(sum(row["seconds"] for row in selected), 3),
                "cost_per_review": sum(costs) / len(costs) if len(costs) == len(selected) and costs else None,
            }
        )
    return totals


def cost_text(value):
    return "n/a" if value is None else f"${value:.4f}"


def print_rows(rows, totals):
    print(f"{'case':<20} {'reviewer':<16} {'recall':>8} {'false positives':>15} {'seconds':>8} {'cost':>10}")
    for row in rows:
        print(
            f"{row['case']:<20} {row['reviewer']:<16} {row['recall']:>8.3f} "
            f"{row['false_positives']:>15} {row['seconds']:>8.3f} {cost_text(row['cost']):>10}"
        )
    for total in totals:
        print(
            f"{'TOTAL':<20} {total['reviewer']:<16} {total['recall']:>8.3f} "
            f"{total['false_positives']:>15} {total['seconds']:>8.3f} "
            f"{cost_text(total['cost_per_review']):>10}"
        )


def build_parser():
    parser = argparse.ArgumentParser(description="Score command line reviewers against golden PR cases.")
    parser.add_argument("--cases", required=True)
    parser.add_argument("--repo", default=str(REPO_ROOT))
    parser.add_argument("--reviewers")
    parser.add_argument("--reviewer", action="append", default=[], type=runner.parse_override)
    parser.add_argument("--price", action="append", default=[], type=parse_price)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    cases_dir = Path(args.cases).resolve()
    repo = Path(args.repo).resolve()
    if not cases_dir.is_dir():
        print(f"Scoring failed: cases directory does not exist: {cases_dir}", file=sys.stderr)
        return 1
    prices = dict(DEFAULT_PRICES)
    prices.update(dict(args.price))
    configs_by_name = runner.load_reviewer_configs(args.reviewer)
    configs, missing = runner.select_reviewers(configs_by_name, args.reviewers)
    if missing:
        print(f"Scoring failed: reviewers are not configured: {', '.join(missing)}", file=sys.stderr)
        return 1
    if not configs:
        print("Scoring failed: no reviewers are configured or selected.", file=sys.stderr)
        return 1
    try:
        rows = score_cases(cases_dir, configs, prices, repo=repo)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Scoring failed: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("Scoring failed: no case directories contain diff.patch.", file=sys.stderr)
        return 1
    totals = reviewer_totals(rows)
    document = {
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "cases": rows,
        "reviewers": totals,
    }
    output = cases_dir / f"scores-{datetime.date.today().isoformat()}.json"
    output.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print_rows(rows, totals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
