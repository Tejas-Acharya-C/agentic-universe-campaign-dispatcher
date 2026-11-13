"""Configuration settings for the Campaign Dispatcher."""

from dataclasses import dataclass, field
import os
from typing import Optional


@dataclass
class Settings:
    provider_url: str = field(
        default_factory=lambda: os.getenv("PROVIDER_URL", "http://127.0.0.1:8099")
    )
    callback_host: str = field(
        default_factory=lambda: os.getenv("CALLBACK_HOST", "127.0.0.1")
    )
    callback_port: int = field(
        default_factory=lambda: int(os.getenv("CALLBACK_PORT", "8200"))
    )
    callback_path: str = "/webhooks/telephony"
    fixtures_path: str = field(
        default_factory=lambda: os.getenv(
            "FIXTURES_PATH", "assignment-b-workflow/fixtures/campaigns.json"
        )
    )
    db_path: str = field(
        default_factory=lambda: os.getenv("DB_PATH", "solution/dispatcher.db")
    )
    output_csv_path: str = field(
        default_factory=lambda: os.getenv("OUTPUT_CSV_PATH", "final_state.csv")
    )
    window_safety_margin_sec: int = field(
        default_factory=lambda: int(os.getenv("WINDOW_SAFETY_MARGIN_SEC", "15"))
    )

    @property
    def callback_url(self) -> str:
        return f"http://{self.callback_host}:{self.callback_port}{self.callback_path}"

    @classmethod
    def from_cli(cls, **overrides) -> "Settings":
        """Construct Settings with optional CLI overrides."""
        cleaned = {k: v for k, v in overrides.items() if v is not None}
        return cls(**cleaned)

