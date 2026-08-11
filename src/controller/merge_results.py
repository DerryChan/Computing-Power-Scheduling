#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, hashlib
from pathlib import Path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", nargs="+", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--reference", default="")
    ap.add_argument("--expect-samples", type=int, default=4096)
    args = ap.parse_args()
    rows = []
    for p in args.parts:
        for line in Path(p).read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    ids = [r["sample_id"] for r in rows]
    missing = args.expect_samples - len(set(ids))
    dup = len(ids) - len(set(ids))
    rows.sort(key=lambda r: r["sample_id"])
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fp:
        for r in rows:
            fp.write(json.dumps(r, ensure_ascii=False) + "\n")
    ref_match = None
    if args.reference and Path(args.reference).exists():
        ref = [json.loads(x) for x in Path(args.reference).read_text().splitlines() if x.strip()]
        ref_map = {r["sample_id"]: r["top1_class"] for r in ref}
        mismatch = sum(1 for r in rows if ref_map.get(r["sample_id"]) != r["top1_class"])
        ref_match = {"compared": len(rows), "mismatch": mismatch, "pass": mismatch == 0 and missing == 0 and dup == 0}
    summary = {
        "samples": len(rows),
        "unique_samples": len(set(ids)),
        "missing": max(0, missing) if missing > 0 else (0 if len(set(ids)) >= args.expect_samples else args.expect_samples - len(set(ids))),
        "duplicates": dup,
        "output_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "reference_match": ref_match,
        "ok": dup == 0 and len(set(ids)) == args.expect_samples and (ref_match is None or ref_match["pass"]),
    }
    summary_path = out.with_name("summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))

if __name__ == "__main__":
    main()
