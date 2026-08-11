#!/usr/bin/env python3
"""Build frozen L1 PoC dataset + ResNet50 weights + reference outputs."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import struct
import tarfile
import zlib
from pathlib import Path

import torch
from torchvision import models

ROOT = Path(os.environ.get("L1_BUNDLE_ROOT", "/opt/l1-poc-bundle"))
DATA = ROOT / "data" / "dataset-140m"
WEIGHTS = ROOT / "weights" / "resnet50_frozen.pth"
APP = ROOT / "app"
REF = ROOT / "reference"
N = 4096
SHARDS = 8


def crc(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def write_jpeg(path: Path, seed: int, size: int = 224):
    """Deterministic colorful synthetic JPEG without PIL dependency for generation speed.
    Uses PIL if available; else writes PPM then relies on pillow later.
    """
    from PIL import Image
    import numpy as np
    rng = np.random.default_rng(seed)
    # structured pattern + noise -> compressible but sizable
    yy, xx = np.mgrid[0:size, 0:size]
    base = ((xx + seed) % 256).astype("float32")
    img = np.stack([
        (base + 40 * np.sin(yy / 9.0) + rng.integers(0, 25, (size, size))) % 256,
        (base * 0.5 + 60 * np.cos(xx / 11.0) + rng.integers(0, 25, (size, size))) % 256,
        ((yy * 3 + seed * 7) % 256 + rng.integers(0, 30, (size, size))) % 256,
    ], axis=-1).astype("uint8")
    Image.fromarray(img, mode="RGB").save(path, format="JPEG", quality=92)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "weights").mkdir(parents=True, exist_ok=True)
    (ROOT / "app").mkdir(parents=True, exist_ok=True)
    REF.mkdir(parents=True, exist_ok=True)
    for i in range(1, SHARDS + 1):
        (DATA / f"part-{i:02d}").mkdir(parents=True, exist_ok=True)

    print("saving frozen resnet50 weights...")
    model = models.resnet50(weights=models.ResNet50_Weights.IMAGENET1K_V2)
    torch.save(model.state_dict(), WEIGHTS)

    print("generating dataset...")
    rows = []
    for i in range(N):
        shard = i % SHARDS + 1
        sample_id = f"S{i:05d}"
        rel = f"part-{shard:02d}/{sample_id}.jpg"
        path = DATA / rel
        write_jpeg(path, seed=202607 + i)
        rows.append({"sample_id": sample_id, "shard_id": f"S{shard:02d}", "path": rel})

    man = DATA / "manifest.csv"
    with open(man, "w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=["sample_id", "shard_id", "path"])
        w.writeheader()
        w.writerows(rows)
    for shard in range(1, SHARDS + 1):
        part_rows = [r for r in rows if r["shard_id"] == f"S{shard:02d}"]
        with open(DATA / f"manifest_part-{shard:02d}.csv", "w", newline="", encoding="utf-8") as fp:
            w = csv.DictWriter(fp, fieldnames=["sample_id", "shard_id", "path"])
            w.writeheader()
            w.writerows(part_rows)

    tar_path = ROOT / "data" / "dataset-140m.tar.gz"
    print("packing tar...")
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(DATA, arcname="dataset-140m")

    meta = {
        "bundle_name": "poc/resnet50-batch-infer:1.0-frozen",
        "samples": N,
        "shards": SHARDS,
        "seed": 202607,
        "weights_sha256": sha256(WEIGHTS),
        "dataset_tar_sha256": sha256(tar_path),
        "dataset_tar_bytes": tar_path.stat().st_size,
        "manifest_sha256": sha256(man),
        "note": "Synthetic non-medical images; ResNet50 ImageNet weights frozen offline.",
    }
    (ROOT / "input_manifest.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    lines = []
    for p in [WEIGHTS, tar_path, man, ROOT / "input_manifest.json"]:
        lines.append(f"{sha256(p)}  {p.relative_to(ROOT)}")
    for shard in range(1, SHARDS + 1):
        p = DATA / f"manifest_part-{shard:02d}.csv"
        lines.append(f"{sha256(p)}  {p.relative_to(ROOT)}")
    (ROOT / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(meta, indent=2))
    print("bundle root", ROOT)


if __name__ == "__main__":
    main()
