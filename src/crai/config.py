"""Settings loaded from the environment (and an optional `.env` file)."""

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_API_BASE = "https://api.clashroyale.com/v1"


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True, slots=True)
class Settings:
    api_token: str | None
    api_base: str
    db_path: Path
    my_tag: str | None


def get_settings() -> Settings:
    # cwd first so a local .env wins, then the project root (the MCP server may start elsewhere).
    _load_dotenv(Path.cwd() / ".env")
    _load_dotenv(PROJECT_ROOT / ".env")
    db = os.environ.get("CR_DB_PATH") or str(PROJECT_ROOT / "data" / "clash.db")
    return Settings(
        api_token=os.environ.get("CR_API_TOKEN") or None,
        api_base=(os.environ.get("CR_API_BASE") or DEFAULT_API_BASE).rstrip("/"),
        db_path=Path(db).expanduser(),
        my_tag=os.environ.get("CR_MY_TAG") or None,
    )
