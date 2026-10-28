"""Small, explicit configuration. No secrets are printed."""

import os
from dataclasses import dataclass
from pathlib import Path


def load_env(path=Path(".env")):
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                os.environ.setdefault(name.strip(), value.strip().strip("\"'"))


@dataclass
class Config:
    database: str = "data/lightpost.sqlite"
    host: str = "127.0.0.1"
    port: int = 3000
    password: str = ""
    key: str = ""
    secure_cookie: bool = False
    api_key: str = ""
    api_enabled: bool = True
    public_url: str = ""  # PUBLIC_URL: an https address you provide yourself
    say: object = print  # Where the public-address instructions are printed.
    interval: float = 3
    scheduler: bool = True
    web_root: str = "web"

    @classmethod
    def from_env(cls):
        load_env()
        config = cls(
            database=os.getenv("DATABASE_PATH", "data/lightpost.sqlite"),
            host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "3000")),
            password=os.getenv("ADMIN_PASSWORD", ""),
            key=os.getenv("ENCRYPTION_KEY", ""),
            web_root=os.getenv("WEB_ROOT", "web"),
            secure_cookie=os.getenv("COOKIE_SECURE") == "true",
            api_key=os.getenv("API_KEY", "").strip(),
            api_enabled=os.getenv("API_ENABLED", "true").strip().lower() != "false",
            public_url=os.getenv("PUBLIC_URL", "").strip(),
        )
        if config.public_url:
            from .remote import clean_public_url

            config.public_url = clean_public_url(config.public_url)
        if config.api_key and len(config.api_key) < 24:
            raise ValueError(
                "API_KEY must be at least 24 characters. Remove it to let LightPost create "
                "and save a key for you (see Settings)."
            )
        if config.host not in ("127.0.0.1", "localhost", "::1") and len(config.password) < 16:
            raise ValueError("Set ADMIN_PASSWORD (16+ characters) before binding beyond loopback.")
        return config
