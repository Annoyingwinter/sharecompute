# 4060 + 4090 simultaneous training test — runbook (2026-10-23 evening)

## Design
- One training run split by data: master dataset seed 42, 8192 sequences.
  Shard 0 = slice [0:4096], shard 1 = slice [4096:8192]. Same TinyTransformer,
  same steps; each shard saves checkpoint.pt + metrics.json, collected by the
  coordinator. Wall time per shard is the comparison metric (4060 vs 4090).
- Farm has NO worker targeting: assignment = first idle worker to poll.
  Pinning trick: start laptop worker alone -> submit shard 0 (laptop-specific
  python path) -> once assigned, start AutoDL worker -> submit shard 1.
  Executions overlap; assignment is pinned.

## Topology
- Coordinator: user's laptop, ~/sharecompute/run1, port 8765, token in
  run1/token.txt (600). Laptop hosts it because VM egress breaks cloudflared
  quick tunnels (TLS first-record error, verified 2026-10-23 18:06).
- Public route for AutoDL worker: cloudflared quick tunnel on the laptop
  (~/sharecompute/bin/cloudflared) -> trycloudflare URL in run1/tunnel_url.txt.
- Submitter (flexing VM) uses the same tunnel URL.

## Commands
Laptop coordinator:
  cd ~/sharecompute/run1 && SHARECOMPUTE_TOKEN=$(cat token.txt) SC_PORT=8765 \
    setsid nohup python3 ~/sharecompute/coordinator.py > coordinator.log 2>&1 &
Laptop tunnel:
  setsid nohup ~/sharecompute/bin/cloudflared tunnel --url http://127.0.0.1:8765 \
    --no-autoupdate > ~/sharecompute/run1/tunnel.log 2>&1 &
Laptop worker (name laptop-4060):
  cd ~/sharecompute && SHARECOMPUTE_TOKEN=$(cat run1/token.txt) setsid nohup \
    python3 worker.py --coordinator <TUNNEL_URL> --name laptop-4060 \
    --workdir ~/sharecompute/sc-work > run1/worker-laptop.log 2>&1 &
Shard jobs: steps = 12000 (matches baseline compute exactly).
Shard 0 (laptop): venv python train_shard.py --shard 0 --num-shards 2 --steps 12000
Shard 1 (AutoDL): python train_shard.py --shard 1 --num-shards 2 --steps 12000
  (submit only after AutoDL worker registered and shard 0 already assigned.)
AutoDL worker (name autodl-4090): worker.py standalone; python = instance
  torch env (probe first). Submitter runs ON the laptop (VM urllib breaks
  on the tunnel's HTTPS via the egress proxy; curl works, python doesn't).

## Measured baseline (2026-10-23 18:20, via the farm itself)
- Smoke: shard 0 steps=30 on 4060, loss 0.0956 -> 0.0155, outputs collected.
- Baseline (num-shards=1, full 8192, 12000 steps, 4060): wall 69.31s,
  train loss 0.0988 -> 0.00116; unified full-data eval loss = 0.00066.
  GPU temp during run peaked 72C (temp-log), fine.
- Steady-state step time on 4060 ~5.8ms (the 27ms in smoke was warmup).
- Soup target: merged model's full-data eval loss vs baseline 0.00066,
  and parallel wall = max(shard walls) vs baseline 69.31s.

## Evidence to collect
- coordinator /workers shows both workers online, each with current_job.
- Both jobs' stdout timestamp ranges overlap by minutes.
- metrics.json from both: gpu names differ (4060 Laptop vs 4090), wall_sec
  compared; loss decreases in both.
- While both run: nvidia-smi snapshot on laptop (util >80%) + AutoDL side
  nvidia-smi via its worker job log or console.

## Gates
- AutoDL shard needs: recharge (arrears -16.02) + boot old 4090 instance.
  User-only step; reminded 16:57, awaiting confirmation.
- Laptop GPU temp: was 85-89C under load before elevating; watch temp-log.csv
  during the run; abort shard if GPU >92C sustained.
