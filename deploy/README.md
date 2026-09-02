# 24/7 deployment

The target host is a small persistent Linux VM. Oracle Cloud Always Free is a suitable no-monthly-cost option when capacity is available.

## What this deployment starts automatically

- TST Fusion Master
- Freqtrade in its existing `dry_run: true` configuration
- persistent Fusion state
- restart-on-reboot Docker services

Hummingbot API + Condor are installed with Hummingbot's official setup script because their current production stack includes PostgreSQL, EMQX, encrypted credentials, and optional Tailscale networking.

## Ubuntu / Oracle VM

After cloning the repo on the VM:

```bash
sudo bash deploy/bootstrap-oracle.sh
sudo bash deploy/install-condor.sh
```

The second command is interactive because Hummingbot's official installer asks for its own API password, encryption password, Telegram credentials, and Tailscale setup.

## Current safety lock

The entire TST Fusion release remains PAPER_ONLY. The Hummingbot config template has:

```yaml
allow_executor_actions: false
```

and the Fusion policy has `executorAllowed: false`. Therefore this deployment does not authorize real Binance orders.

## Files

- `deploy/docker-compose.yml` — persistent Fusion + Freqtrade services
- `deploy/Dockerfile.fusion` — Fusion Master image
- `deploy/.env.example` — secret placeholders
- `deploy/bootstrap-oracle.sh` — server bootstrap
- `deploy/install-condor.sh` — official Hummingbot API + Condor installer
- `hummingbot/configs/tst_fusion_signal_btc.yml` — paper-only controller template

Do not place exchange or Telegram secrets in Git.
