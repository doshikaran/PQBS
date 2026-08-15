"""pqbs demo launcher — thin orchestration only.

Validates env, seeds demo data if needed, starts the FastAPI backend that
serves the pre-built React SPA, and opens the browser.

This file is a launcher, not a feature surface. Do not add new visualization
logic, new API endpoints, or new configuration here. All of that belongs in
demo/ui/app.py (API) or demo/ui/frontend/src/ (React).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _find_repo_root() -> Path:
    """Locate the PQBS repo root by walking up for pyproject.toml."""
    # Editable install: __file__ is src/pqbs/cli/launcher.py → 3 levels up
    candidate = Path(__file__).resolve().parents[3]
    if (candidate / "pyproject.toml").exists():
        return candidate
    # Fallback: walk up from cwd
    for p in [Path.cwd(), *Path.cwd().parents]:
        if (p / "pyproject.toml").exists():
            return p
    raise SystemExit(
        "ERROR: Cannot find PQBS repo root (no pyproject.toml found).\n"
        "Run pqbs from within the cloned repository."
    )


REPO_ROOT = _find_repo_root()


def _check_env() -> tuple[str, str]:
    """Return (cockroach_url, tenant_id) or exit with a clear message."""
    missing: list[str] = []
    url = os.environ.get("COCKROACH_URL", "").strip()
    tenant = os.environ.get("TENANT_ID_DEMO", "").strip()
    if not url:
        missing.append("COCKROACH_URL")
    if not tenant:
        missing.append("TENANT_ID_DEMO")
    if missing:
        print(
            f"ERROR: Missing required environment variable(s): {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill in the values, then re-run pqbs.",
            file=sys.stderr,
        )
        sys.exit(1)
    return url, tenant


def _test_connection(url: str) -> None:
    """Verify DB is reachable. Exit with a specific error if not."""
    try:
        import psycopg
        conn = psycopg.connect(url, autocommit=True, connect_timeout=10)
        conn.execute("SELECT 1")
        conn.close()
    except Exception as exc:
        print(
            f"ERROR: Cannot connect to CockroachDB.\n"
            f"  Check COCKROACH_URL in .env\n"
            f"  Detail: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


def _count_demo_data(url: str, tenant_id: str) -> tuple[int, int]:
    """Return (belief_count, quarantine_count) for the demo tenant."""
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    try:
        b = conn.execute(
            "SELECT COUNT(*) AS n FROM belief WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()
        q = conn.execute(
            "SELECT COUNT(*) AS n FROM quarantine WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()
        return int(b["n"]), int(q["n"])  # type: ignore[index]
    finally:
        conn.close()


def _run_seed(reset: bool) -> None:
    """Invoke scripts/seed_demo.py as a subprocess (streams progress to stdout)."""
    seed_script = REPO_ROOT / "scripts" / "seed_demo.py"
    if not seed_script.exists():
        print(
            f"ERROR: Seed script not found at {seed_script}\n"
            f"Is this a complete PQBS checkout?",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = [sys.executable, str(seed_script)]
    if reset:
        cmd.append("--reset")

    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        print("ERROR: Seeding failed. See output above.", file=sys.stderr)
        sys.exit(1)


def _start_server(port: int) -> None:
    """Run uvicorn blocking (called from main — never returns normally)."""
    # Add repo root so `import demo.ui.app` resolves
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    dist = REPO_ROOT / "demo" / "ui" / "frontend" / "dist"
    if not dist.exists():
        print(
            f"ERROR: Built frontend not found at {dist}\n"
            f"Run: cd demo/ui/frontend && npm install && npm run build",
            file=sys.stderr,
        )
        sys.exit(1)

    import uvicorn

    uvicorn.run(
        "demo.ui.app:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )


def _open_browser_after_delay(url: str, delay: float = 1.5) -> None:
    """Open the browser after a short delay so uvicorn has time to bind."""
    time.sleep(delay)
    webbrowser.open(url)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pqbs",
        description="Start the PQBS demo UI (seeds data, opens browser).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Wipe and reseed the demo tenant before starting.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        dest="no_browser",
        help="Start everything but do not auto-open a browser window.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port for the demo server (default: 8080).",
    )
    args = parser.parse_args()

    # 1. Load .env from repo root
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")

    # 2. Validate required env vars
    url, tenant_id = _check_env()

    # 3. Test DB connectivity — fail fast with a human-readable error
    print("Connecting to CockroachDB...", end=" ", flush=True)
    _test_connection(url)
    print("OK")

    # 4. Seed if needed
    if args.reset:
        print("Resetting demo tenant and reseeding...")
        _run_seed(reset=True)
    else:
        belief_count, _ = _count_demo_data(url, tenant_id)
        if belief_count == 0:
            print("No demo data found — seeding for the first time...")
            _run_seed(reset=False)
        else:
            print(f"Demo data present ({belief_count} beliefs). Use --reset to reseed.")

    # 5. Final counts for status line
    n_beliefs, n_quarantined = _count_demo_data(url, tenant_id)

    # 6. Kick off browser opener in background (uvicorn needs ~1s to bind)
    local_url = f"http://localhost:{args.port}"
    if not args.no_browser:
        threading.Thread(
            target=_open_browser_after_delay,
            args=(local_url,),
            daemon=True,
        ).start()

    print(
        f"Connected to cluster · {n_beliefs} beliefs ({n_quarantined} quarantined) · {local_url}"
    )

    # 7. Start server (blocks until Ctrl-C)
    _start_server(args.port)


if __name__ == "__main__":
    main()
