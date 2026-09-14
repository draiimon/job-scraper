from __future__ import annotations
import json
import os
from pathlib import Path
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import dotenv_values

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./data/job_agent.sqlite3"
    discord_webhook_url: str | None = None
    discord_motivation: str = "PU IS TANG IS NA IS MO! MAG APPLY KANA NG WORK KUNG AYAW MO MAGING UNEMPLOYED!"
    min_notify_score: int = 70
    min_auto_application_score: int = 85
    auto_send_email_applications: bool = False
    polling_enabled: bool = True
    poll_interval_seconds: int = 900
    app_timezone: str = "Asia/Manila"
    app_secret_key: str | None = None
    public_base_url: str | None = None
    cover_letter_mode: str = "template"
    source_targets_json: str = ""
    source_config_path: str = "config/job_sources.json"
    google_client_id: str | None = None
    google_client_secret: str | None = None
    profile_path: str = "data/master-profile.json"
    ai_enabled: bool = False
    ai_provider: str | None = None
    ai_api_key: str | None = None
    ai_model: str | None = None
    ai_daily_request_limit: int = 20
    ai_min_job_score: int = 75
    gemini_key_projects_json: str = ""
    gemini_model: str = "gemini-3.8-flash"
    gemini_max_retries: int = 3
    gemini_concurrency_limit: int = 2

    @property
    def source_targets(self) -> list[dict]:
        if self.source_targets_json.strip():
            try: return json.loads(self.source_targets_json)
            except json.JSONDecodeError: return []
        try: return json.loads(Path(self.source_config_path).read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError): return []
    @property
    def gemini_keys(self) -> list[tuple[str,str]]:
        values={**dotenv_values('.env'), **os.environ}; keys=[]
        combined=str(values.get('GEMINI_API_KEYS') or '')
        for index,key in enumerate((x.strip() for x in combined.replace('\n',',').split(',')),start=1):
            if key: keys.append((f'list-{index}',key))
        for index in range(1,10):
            name='GEMINI_API_KEY' if index==1 else f'GEMINI_API_KEY{index}'
            if values.get(name): keys.append((str(index),str(values[name])))
        unique=[]; seen=set()
        for index,key in keys:
            if key not in seen: unique.append((index,key)); seen.add(key)
        return unique
    @property
    def gemini_projects(self) -> dict[str,str]:
        try: return {str(k):str(v) for k,v in json.loads(self.gemini_key_projects_json).items()}
        except json.JSONDecodeError: return {}

@lru_cache
def settings() -> Settings: return Settings()
