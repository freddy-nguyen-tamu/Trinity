"""Verified v13 benchmark-residual PPO with causal volatility-aware cash."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for path in (ROOT / "rl_trading" / "src", ROOT / "rl_trading"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def main() -> int:
    from rl_trading.experiment.strategy_contract import verify_v13_contract
    # v13 changes the action-to-cash meaning relative to previous runs.
    if any(arg == "--warm-start-from" or arg.startswith("--warm-start-from=")
           for arg in sys.argv[1:]):
        raise SystemExit("v13 uses a different cash mapping; train fresh without --warm-start-from")
    try:
        provenance = verify_v13_contract()
    except (ImportError, TypeError, ValueError, RuntimeError, FileNotFoundError) as exc:
        raise SystemExit(f"[CSCEDRL] Strategy verification FAILED: {exc}") from exc
    print("[CSCEDRL] Strategy verified: " + provenance["strategy_id"], flush=True)
    print("[CSCEDRL] Source/config SHA-256 fingerprints will be archived.", flush=True)
    from rl_trading.workflow import main as run_main
    return run_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
