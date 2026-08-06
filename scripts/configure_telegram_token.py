"""Securely store and validate a Telegram bot token in the local .env file."""

from getpass import getpass
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
TOKEN_PATTERN = re.compile(r"^[1-9][0-9]{5,14}:[A-Za-z0-9_-]{30,}$")


def validate_with_telegram(token: str) -> str:
    try:
        with urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=15) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError("Telegram tokenini tekshirib bo'lmadi") from exc
    if not payload.get("ok"):
        raise ValueError("Telegram tokeni qabul qilinmadi")
    return str(payload["result"]["username"])


def store_token(token: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    updated = [
        f"TELEGRAM_BOT_TOKEN={token}" if line.startswith("TELEGRAM_BOT_TOKEN=") else line
        for line in lines
    ]
    ENV_PATH.write_text("\n".join(updated) + "\n", encoding="utf-8")
    ENV_PATH.chmod(0o600)


def main() -> None:
    if not ENV_PATH.exists():
        raise SystemExit(".env topilmadi")
    token = getpass("BotFather tokenini kiriting (ekranda ko'rinmaydi): ").strip()
    if not TOKEN_PATTERN.fullmatch(token):
        raise SystemExit("Token formati noto'g'ri")
    try:
        username = validate_with_telegram(token)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    store_token(token)
    print(f"Tayyor: @{username} tokeni .env ichiga xavfsiz saqlandi.")


if __name__ == "__main__":
    main()
