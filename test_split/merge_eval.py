#!/usr/bin/env python3
"""Model-soup merge + unified evaluation for the split-training test.

Merges two shard checkpoints by averaging weights (same init, disjoint data
slices), then evaluates any checkpoint on the FULL master dataset so the
numbers are comparable: shard0-only vs shard1-only vs merged vs single-card
full-data baseline.

Usage:
  merge_eval.py --a shard0/checkpoint.pt --b shard1/checkpoint.pt \
      --out merged.pt --eval shard0/checkpoint.pt shard1/checkpoint.pt merged.pt
"""
import argparse
import json
import sys
import time
from pathlib import Path

import torch
import torch.nn as nn

sys.path.insert(0, str(Path(__file__).resolve().parent))
from train_shard import TinyTransformer, build_master_data


def load_model(path, device):
    model = TinyTransformer().to(device)
    state = torch.load(path, map_location=device, weights_only=True)
    model.load_state_dict(state)
    model.eval()
    return model


def evaluate(model, x, y, device, batch=512):
    lossf = nn.MSELoss()
    total, n = 0.0, 0
    with torch.no_grad():
        for i in range(0, x.shape[0], batch):
            xb, yb = x[i:i + batch].to(device), y[i:i + batch].to(device)
            loss = lossf(model(xb), yb)
            total += loss.item() * xb.shape[0]
            n += xb.shape[0]
    return total / n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a")
    ap.add_argument("--b")
    ap.add_argument("--out")
    ap.add_argument("--eval", nargs="*", default=[])
    args = ap.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    x, y = build_master_data()

    if args.a and args.b and args.out:
        sa = torch.load(args.a, map_location="cpu", weights_only=True)
        sb = torch.load(args.b, map_location="cpu", weights_only=True)
        merged = {k: (sa[k].float() + sb[k].float()) / 2.0 for k in sa}
        torch.save(merged, args.out)
        print(f"merged {args.a} + {args.b} -> {args.out}", flush=True)

    results = {}
    for path in args.eval:
        model = load_model(path, device)
        t0 = time.time()
        results[path] = round(evaluate(model, x, y, device), 5)
        print(f"eval {path}: full-data loss {results[path]} "
              f"({time.time()-t0:.1f}s)", flush=True)
    Path("merge_eval_results.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
