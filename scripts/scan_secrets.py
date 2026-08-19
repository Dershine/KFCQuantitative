from __future__ import annotations

import argparse
from pathlib import Path

from kfcops.supply_chain import scan_paths, tracked_paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan tracked source files for high-confidence secrets")
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--tracked", action="store_true", help="scan files tracked by Git")
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    paths = tracked_paths(root) if arguments.tracked else arguments.paths
    if not paths:
        parser.error("provide paths or --tracked")
    findings = scan_paths(paths)
    for finding in findings:
        try:
            display = finding.path.relative_to(root)
        except ValueError:
            display = finding.path
        print(f"{display}:{finding.line_number}: {finding.rule}: [REDACTED]")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
