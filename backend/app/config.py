from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mock_ai: bool = True
    yandex_api_key: str = ""
    yandex_folder_id: str = ""
    yandexgpt_model: str = "yandexgpt"
    database_url: str = f"sqlite:///{ROOT_DIR / 'data' / 'speechai.db'}"
    audio_dir: Path = ROOT_DIR / "data" / "audio"
    evaluation_prompt_primary_path: Path = ROOT_DIR / "config" / "evaluation_prompt_primary_adult.txt"
    evaluation_prompt_repeat_path: Path = ROOT_DIR / "config" / "evaluation_prompt_repeat_adult.txt"
    session_secret: str = "speechai-mvp-secret"


@lru_cache
def get_settings() -> Settings:
    return Settings()
