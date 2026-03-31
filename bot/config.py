import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    ALLOWED_USER_IDS: list[int] = [
        int(uid.strip())
        for uid in os.getenv("ALLOWED_USER_IDS", "").split(",")
        if uid.strip()
    ]

    # Domain
    BASE_DOMAIN: str = os.getenv("BASE_DOMAIN", "example.com")

    # Cloudflare
    TUNNEL_UUID: str = os.getenv("TUNNEL_UUID", "")
    CLOUDFLARED_CONFIG_PATH: str = os.getenv(
        "CLOUDFLARED_CONFIG_PATH", "/etc/cloudflared/config.yml"
    )
    CLOUDFLARED_CREDENTIALS: str = os.getenv("CLOUDFLARED_CREDENTIALS", "")

    # Paths
    PROJECTS_DIR: Path = Path(os.getenv("PROJECTS_DIR", "/opt/appfactory-bot/projects"))
    TEMPLATES_DIR: Path = Path(
        os.getenv("TEMPLATES_DIR", "/opt/appfactory-bot/templates")
    )

    # Claude
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

    # Voice / Transcription
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")  # For Whisper API (optional)
    TEMP_DIR: Path = Path(os.getenv("TEMP_DIR", "/tmp/appfactory"))

    # Docker
    PORT_RANGE_START: int = int(os.getenv("PORT_RANGE_START", "9000"))
    PORT_RANGE_END: int = int(os.getenv("PORT_RANGE_END", "9100"))

    # DB
    DB_PATH: Path = Path(
        os.getenv("DB_PATH", "/opt/appfactory-bot/data/projects.db")
    )

    @classmethod
    def validate(cls) -> list[str]:
        errors = []
        if not cls.TELEGRAM_BOT_TOKEN:
            errors.append("TELEGRAM_BOT_TOKEN is required")
        if not cls.ALLOWED_USER_IDS:
            errors.append("ALLOWED_USER_IDS is required (security!)")
        if not cls.TUNNEL_UUID:
            errors.append("TUNNEL_UUID is required")
        return errors


config = Config()
