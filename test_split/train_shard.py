#!/usr/bin/env python3
"""ShareCompute simultaneous-training test: one shard of a split training job.

A single "training run" is split by data: a master synthetic sequence dataset
(deterministic, seed 42) is generated in full, then this shard trains only on
its slice (shard k of n). Each shard trains the same small Transformer model
with real gradient steps on GPU, logs timestamped loss lines, and writes
metrics.json + checkpoint.pt as outputs for the coordinator to collect.

Usage: train_shard.py --shard 0 --num-shards 2 --steps 600 --outdir .
"""
import argparse
import json
import time
from pathlib import Path

import torch
import torch.nn as nn


def build_master_data(seed=42, total=8192, seq=48, dim=16):
    g = torch.Generator().manual_seed(seed)
    x = torch.randn(total, seq, dim, generator=g)
    # Target: a fixed nonlinear function of the sequence (same for all shards).
    w = torch.randn(dim, dim, generator=g)
    y = torch.tanh(x @ w).mean(dim=1)
    return x, y


class TinyTransformer(nn.Module):
    def __init__(self, dim=16, heads=4, layers=3):
        super().__init__()
        layer = nn.TransformerEncoderLayer(
            d_model=dim, nhead=heads, dim_feedforward=128, batch_first=True)
        self.enc = nn.TransformerEncoder(layer, num_layers=layers)
        self.head = nn.Linear(dim, dim)

    def forward(self, x):
        return self.head(self.enc(x).mean(dim=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, required=True)
    ap.add_argument("--num-shards", type=int, default=2)
    ap.add_argument("--steps", type=int, default=600)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--outdir", default=".")
    args = ap.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    gpu_name = torch.cuda.get_device_name(0) if device == "cuda" else "cpu"
    print(f"[{time.strftime('%H:%M:%S')}] shard {args.shard}/{args.num_shards} "
          f"start on {gpu_name} steps={args.steps}", flush=True)

    x, y = build_master_data()
    per = x.shape[0] // args.num_shards
    lo, hi = args.shard * per, (args.shard + 1) * per
    xs, ys = x[lo:hi].to(device), y[lo:hi].to(device)
    print(f"data slice [{lo}:{hi}] of {x.shape[0]}", flush=True)

    # Fixed init seed: every shard starts from IDENTICAL weights, which is
    # what makes weight averaging (model soup) meaningful afterwards.
    torch.manual_seed(1234)
    model = TinyTransformer().to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=2e-3)
    lossf = nn.MSELoss()

    t0 = time.time()
    first_loss = last_loss = None
    for step in range(1, args.steps + 1):
        idx = torch.randint(0, xs.shape[0], (args.batch,), device=device)
        pred = model(xs[idx])
        loss = lossf(pred, ys[idx])
        opt.zero_grad()
        loss.backward()
        opt.step()
        if first_loss is None:
            first_loss = loss.item()
        last_loss = loss.item()
        if step % 50 == 0 or step == 1:
            print(f"[{time.strftime('%H:%M:%S')}] step {step}/{args.steps} "
                  f"loss {last_loss:.4f} elapsed {time.time()-t0:.1f}s",
                  flush=True)
    if device == "cuda":
        torch.cuda.synchronize()
    wall = time.time() - t0

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), out / "checkpoint.pt")
    metrics = {
        "shard": args.shard,
        "num_shards": args.num_shards,
        "gpu": gpu_name,
        "steps": args.steps,
        "data_slice": [lo, hi],
        "first_loss": first_loss,
        "last_loss": last_loss,
        "wall_sec": round(wall, 2),
        "start_epoch": t0,
    }
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(f"[{time.strftime('%H:%M:%S')}] shard {args.shard} DONE "
          f"wall {wall:.1f}s loss {first_loss:.4f} -> {last_loss:.4f}",
          flush=True)


if __name__ == "__main__":
    main()
