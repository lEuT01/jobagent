"""Configuration: environment variables, overridden by data/settings.json (written from the web UI)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

import yaml

SECRET_KEYS = ("anthropic_api_key", "adzuna_app_id", "adzuna_app_key", "reed_api_key", "mail_password")
MAIL_KEYS = ("mail_imap_host", "mail_smtp_host", "mail_user", "mail_folder", "mail_notify_to", "mail_lookback_days", "mail_poll_minutes", "mail_notify_email")


@dataclass
class Settings:
    anthropic_api_key: str | None = None
    adzuna_app_id: str | None = None
    adzuna_app_key: str | None = None
    reed_api_key: str | None = None
    mail_imap_host: str = "imap.gmail.com"
    mail_smtp_host: str = "smtp.gmail.com"
    mail_user: str = ""
    mail_password: str | None = None
    mail_folder: str = "INBOX"
    mail_notify_to: str = ""
    mail_notify_email: bool = False
    mail_lookback_days: int = 30
    mail_poll_minutes: int = 10
    profile_model: str = "claude-sonnet-5-5"
    score_model: str = "claude-haiku-5-5"
    data_dir: Path = field(default_factory=lambda: Path("data"))

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls(
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
            adzuna_app_id=os.getenv("ADZUNA_APP_ID") or None,
            adzuna_app_key=os.getenv("ADZUNA_APP_KEY") or None,
            reed_api_key=os.getenv("REED_API_KEY") or None,
            profile_model=os.getenv("JOBAGENT_PROFILE_MODEL", "claude-sonnet-5-5"),
            score_model=os.getenv("JOBAGENT_SCORE_MODEL", "claude-haiku-5-5"),
            data_dir=Path(os.getenv("JOBAGENT_DATA_DIR", "data")),
        )
        f = s.data_dir / "settings.json"
        if f.exists():
            try:
                saved = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                saved = {}
            for k, v in saved.items():
                if v not in (None, "") and hasattr(s, k) and k != "data_dir":
                    setattr(s, k, v)
        return s

    def save(self, updates: dict) -> None:
        f = self.data_dir / "settings.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        current = {}
        if f.exists():
            try:
                current = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                current = {}
        for k, v in updates.items():
            if k in SECRET_KEYS:
                if v:
                    current[k] = v
                    setattr(self, k, v)
                elif v == "":          # explicit clear
                    current.pop(k, None)
                    setattr(self, k, None)
            elif k in MAIL_KEYS or k in ("profile_model", "score_model"):
                current[k] = v
                setattr(self, k, v)
        f.write_text(json.dumps(current, indent=2), encoding="utf-8")

    def public(self) -> dict:
        d = asdict(self)
        d["data_dir"] = str(self.data_dir)
        for k in SECRET_KEYS:
            v = d.get(k)
            d[k] = (v[:6] + "…" + v[-3:]) if v and len(v) > 12 and k != "mail_password" else ("set" if v else "")
        return d


def load_yaml(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
