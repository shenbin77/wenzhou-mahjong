"""Audit JSONL / actual Mahjong SQLite schemas; sources are never modified."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dataset_audit import audit_dataset


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jsonl", action="append", default=[], help="Existing game JSONL; repeat for multiple files")
    parser.add_argument("--sqlite", action="append", default=[], help="Existing SQLite database, read-only; repeatable")
    parser.add_argument("--max-records", type=int, default=1000, help="Maximum nonblank JSONL rows / SQLite rounds per source")
    parser.add_argument("--max-plays", type=int, default=10000)
    parser.add_argument("--evidence-root", help="Explicit base for relative source and evidence paths")
    parser.add_argument("--output", help="New report file; omitted prints full JSON to stdout")
    args = parser.parse_args(argv)
    if not (args.jsonl or args.sqlite):
        parser.error("supply --jsonl and/or --sqlite")
    if args.output:
        output = Path(args.output).resolve()
        if output in {Path(path).resolve() for path in args.jsonl + args.sqlite}:
            parser.error("report output must not overwrite an input")
        if output.exists():
            parser.error("report output already exists; choose a new report path")
    try:
        report = audit_dataset(jsonl_paths=args.jsonl, sqlite_paths=args.sqlite,
                               max_records=args.max_records, max_plays=args.max_plays,
                               evidence_root=args.evidence_root)
    except ValueError as exc:
        parser.error(str(exc))
    if args.output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(output), **report["summary"]}, ensure_ascii=False))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report["summary"]["sources_with_issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
