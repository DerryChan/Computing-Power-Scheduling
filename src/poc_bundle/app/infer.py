#!/usr/bin/env python3
"""Frozen ResNet-50 batch inference for L1 PoC."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from pathlib import Path

import torch
import torch.nn as nn
from PIL import Image
from torch.utils.data import DataLoader, Dataset
from torchvision import models, transforms


class ManifestDataset(Dataset):
    def __init__(self, rows, root: Path):
        self.rows = rows
        self.root = root
        self.tf = transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ])

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, idx):
        row = self.rows[idx]
        path = self.root / row["path"]
        img = Image.open(path).convert("RGB")
        return self.tf(img), row["sample_id"], row["shard_id"]


def load_model(weights: Path, device: torch.device) -> nn.Module:
    model = models.resnet50(weights=None)
    state = torch.load(weights, map_location="cpu")
    model.load_state_dict(state)
    model.eval().to(device)
    return model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--weights", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--seed", type=int, default=202607)
    ap.add_argument("--deterministic", action="store_true")
    ap.add_argument("--gpu", type=int, default=0)
    args = ap.parse_args()

    torch.manual_seed(args.seed)
    if args.deterministic:
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    device = torch.device(f"cuda:{args.gpu}" if torch.cuda.is_available() else "cpu")
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    rows = list(csv.DictReader(open(args.manifest, newline="")))
    ds = ManifestDataset(rows, Path(args.data_root))
    loader = DataLoader(ds, batch_size=args.batch_size, shuffle=False, num_workers=2, pin_memory=device.type == "cuda")
    model = load_model(Path(args.weights), device)

    pred_path = out / "predictions.jsonl"
    t0 = time.time()
    n = 0
    with torch.inference_mode(), open(pred_path, "w", encoding="utf-8") as fp:
        for images, sample_ids, shard_ids in loader:
            images = images.to(device, non_blocking=True)
            logits = model(images)
            probs = torch.softmax(logits, dim=1)
            top1 = torch.argmax(probs, dim=1)
            top1p = probs.gather(1, top1.unsqueeze(1)).squeeze(1)
            for sid, shard, cls, conf in zip(sample_ids, shard_ids, top1.tolist(), top1p.tolist()):
                rec = {
                    "sample_id": sid,
                    "shard_id": shard,
                    "top1_class": int(cls),
                    "top1_prob": round(float(conf), 6),
                }
                fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
    elapsed = time.time() - t0
    digest = hashlib.sha256(pred_path.read_bytes()).hexdigest()
    metrics = {
        "samples": n,
        "elapsed_sec": round(elapsed, 3),
        "device": str(device),
        "gpu": args.gpu,
        "batch_size": args.batch_size,
        "seed": args.seed,
        "predictions_sha256": digest,
        "weights_sha256": hashlib.sha256(Path(args.weights).read_bytes()).hexdigest(),
        "ok": True,
    }
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False))


if __name__ == "__main__":
    main()
