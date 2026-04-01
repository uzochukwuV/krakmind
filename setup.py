#!/usr/bin/env python3
"""
ArbMind setup script — validates environment, installs deps, checks CLI.
Run once before starting the agent: python setup.py
"""

import os
import sys
import subprocess
import shutil

REQUIRED_KEYS = [
    ("ANTHROPIC_API_KEY", "Get from https://console.anthropic.com"),
    ("CMC_API_KEY", "Get from https://coinmarketcap.com/api/"),
]

OPTIONAL_KEYS = [
    ("KRAKEN_API_KEY", "Needed for live authenticated endpoints"),
    ("KRAKEN_FUTURES_API_KEY", "Needed for live futures — not required for paper mode"),
]

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg):   print(f"{GREEN}  ✓ {msg}{RESET}")
def warn(msg): print(f"{YELLOW}  ⚠ {msg}{RESET}")
def err(msg):  print(f"{RED}  ✗ {msg}{RESET}")
def header(msg): print(f"\n{BOLD}{msg}{RESET}")


def check_python():
    header("Python version")
    if sys.version_info < (3, 11):
        err(f"Python 3.11+ required. Got {sys.version}")
        sys.exit(1)
    ok(f"Python {sys.version.split()[0]}")


def install_deps():
    header("Installing Python dependencies")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "-q"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        ok("All pip packages installed")
    else:
        err(f"pip install failed:\n{result.stderr}")
        sys.exit(1)


def check_kraken_cli():
    header("Kraken CLI binary")
    if shutil.which("kraken"):
        result = subprocess.run(["kraken", "--version"], capture_output=True, text=True)
        ok(f"Found: {result.stdout.strip()}")
    else:
        warn("kraken CLI binary not found on PATH")
        print(f"""
  {YELLOW}Install it from: https://github.com/krakenfx/kraken-cli/releases{RESET}

  Quick install (Linux/Mac):
    # Download the latest release binary for your platform
    # Add to PATH, e.g.:
    chmod +x kraken
    sudo mv kraken /usr/local/bin/

  Without the CLI, the agent will use python-kraken-sdk REST fallback
  for market data, but paper trading commands need the CLI binary.
""")


def check_env():
    header("Environment variables (.env)")
    if not os.path.exists(".env"):
        if os.path.exists(".env.example"):
            warn(".env file not found. Copying .env.example → .env")
            import shutil
            shutil.copy(".env.example", ".env")
            print(f"  {YELLOW}→ Edit .env and fill in your API keys before running.{RESET}")
        else:
            err(".env and .env.example both missing")
        return

    from dotenv import load_dotenv
    load_dotenv()

    all_ok = True
    for key, hint in REQUIRED_KEYS:
        val = os.getenv(key, "")
        if val and not val.startswith("your_"):
            ok(f"{key} set")
        else:
            err(f"{key} missing or placeholder — {hint}")
            all_ok = False

    for key, hint in OPTIONAL_KEYS:
        val = os.getenv(key, "")
        if val and not val.startswith("your_"):
            ok(f"{key} set (optional)")
        else:
            warn(f"{key} not set — {hint}")

    if not all_ok:
        print(f"\n  {RED}Fix required keys in .env before running main.py{RESET}")


def create_dirs():
    header("Creating data directories")
    for d in ["data", "data/journal", "logs"]:
        os.makedirs(d, exist_ok=True)
    ok("data/, data/journal/, logs/ ready")


def test_cmc():
    header("Testing CoinMarketCap connection")
    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.getenv("CMC_API_KEY", "")
    if not api_key or api_key.startswith("your_"):
        warn("CMC_API_KEY not set — skipping test")
        return

    try:
        import requests
        resp = requests.get(
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest",
            headers={"X-CMC_PRO_API_KEY": api_key},
            params={"limit": 3},
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            coins = [c["symbol"] for c in data.get("data", [])]
            ok(f"CMC API live — top 3: {', '.join(coins)}")
        else:
            err(f"CMC API error: {resp.status_code} — {resp.text[:100]}")
    except Exception as e:
        err(f"CMC connection failed: {e}")


def test_kraken_rest():
    header("Testing Kraken REST API (public, no auth needed)")
    try:
        import requests
        resp = requests.get(
            "https://api.kraken.com/0/public/Ticker?pair=XXBTZUSD",
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            price = float(data["result"]["XXBTZUSD"]["c"][0])
            ok(f"Kraken REST live — BTC price: ${price:,.2f}")
        else:
            err(f"Kraken REST error: {resp.status_code}")
    except Exception as e:
        err(f"Kraken REST failed: {e}")


def print_next_steps():
    print(f"""
{BOLD}{'='*55}{RESET}
{BOLD}  Setup complete. Next steps:{RESET}
{'='*55}

  1. Edit {YELLOW}.env{RESET} with your real API keys

  2. Install Kraken CLI binary:
     {YELLOW}https://github.com/krakenfx/kraken-cli/releases{RESET}

  3. Initialise Kraken CLI config:
     {YELLOW}kraken setup{RESET}

  4. Start the agent (paper mode):
     {GREEN}python main.py{RESET}

  5. View performance summary anytime:
     {GREEN}python data/journal.py{RESET}

{BOLD}Paper trading only — no real money until you set PAPER_MODE=false{RESET}
{'='*55}
""")


if __name__ == "__main__":
    print(f"{BOLD}\n  ArbMind Setup{RESET}")
    print("  AI-first Kraken paper trading agent\n")
    check_python()
    install_deps()
    check_kraken_cli()
    check_env()
    create_dirs()
    test_cmc()
    test_kraken_rest()
    print_next_steps()
