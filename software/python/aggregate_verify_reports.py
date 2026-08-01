"""
verify_reports/ 配下の検証レポート MD (YAML フロントマター) を読み取り、
1 行 1 台の CSV にまとめる。

使い方:
    python aggregate_verify_reports.py
        -> stdout に CSV を出力
    python aggregate_verify_reports.py -o summary.csv
        -> ファイルへ書き出し
    python aggregate_verify_reports.py --dir ../other_reports
        -> ディレクトリを指定

フロントマターは "key: value" の単純形式のみを扱う (本プロジェクト用の
最小実装。PyYAML 等の外部依存は不要)。
"""
import argparse
import csv
import sys
from pathlib import Path

CSV_COLUMNS = [
    "device_id", "firmware_version", "tested_at", "result",
    "temperature_c", "humidity_pct", "velocity_mps", "illuminance_lx", "co2_ppm",
    "temp_check", "humidity_check",
    "velocity_comm", "illuminance_comm", "co2_comm",
]

DEFAULT_DIR = Path(__file__).resolve().parent / "verify_reports"


def parse_frontmatter(text: str):
    """先頭の '---' から次の '---' までを YAML フロントマターとしてパースする。
    フロントマター不在/不正なら None を返す。"""
    # 改行コードを LF に正規化
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        return None

    end_idx = text.find("\n---", 4)
    if end_idx == -1:
        return None

    block = text[4:end_idx]
    out = {}
    for line in block.split("\n"):
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        out[key.strip()] = value.strip()
    return out


def collect_reports(directory: Path):
    rows = []
    for md_path in sorted(directory.glob("*.md")):
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"Warning: Could not read {md_path}: {e}", file=sys.stderr)
            continue

        fm = parse_frontmatter(text)
        if fm is None:
            print(f"Warning: No YAML frontmatter in {md_path.name}, skipping",
                  file=sys.stderr)
            continue

        rows.append(fm)
    return rows


def main():
    p = argparse.ArgumentParser(
        description="Aggregate verify report MD files into a single CSV.")
    p.add_argument("--dir", default=str(DEFAULT_DIR),
                   help=f"Directory containing MD reports (default: {DEFAULT_DIR})")
    p.add_argument("-o", "--output", default=None,
                   help="Output CSV path (default: stdout)")
    args = p.parse_args()

    directory = Path(args.dir)
    if not directory.is_dir():
        print(f"Error: Directory not found: {directory}", file=sys.stderr)
        return 1

    rows = collect_reports(directory)
    if not rows:
        print(f"Warning: No valid report MDs found in {directory}",
              file=sys.stderr)

    if args.output:
        out_file = open(args.output, "w", newline="", encoding="utf-8")
        close_after = True
    else:
        out_file = sys.stdout
        close_after = False

    try:
        writer = csv.DictWriter(out_file, fieldnames=CSV_COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_COLUMNS})
    finally:
        if close_after:
            out_file.close()

    if args.output:
        print(f"Wrote {len(rows)} row(s) to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
