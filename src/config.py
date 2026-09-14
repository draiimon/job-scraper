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
    discord_motivation: str = ""
    discord_motivations_json: str = ""
    min_notify_score: int = 70
    min_auto_application_score: int = 85
    max_notifications_per_cycle: int = 3
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
    brightdata_enabled: bool = False
    brightdata_api_token: str | None = None
    brightdata_linkedin_jobs_dataset_id: str | None = None
    brightdata_jobstreet_dataset_id: str | None = None
    brightdata_linkedin_jobs_inputs_json: str = '[]'
    brightdata_jobstreet_inputs_json: str = '[]'

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
    @property
    def discord_motivations(self) -> list[str]:
        try:
            configured=json.loads(self.discord_motivations_json)
            if isinstance(configured,list) and all(isinstance(x,str) and x.strip() for x in configured): return configured
        except json.JSONDecodeError: pass
        return [
            '𝐏𝐔𝐓𝐀𝐍𝐆 𝐈𝐍𝐀 𝐌𝐎! 𝐌𝐀𝐆-𝐀𝐏𝐏𝐋𝐘 𝐊𝐀 𝐍𝐀 𝐍𝐆 𝐖𝐎𝐑𝐊 𝐊𝐔𝐍𝐆 𝐀𝐘𝐀𝐖 𝐌𝐎 𝐌𝐀𝐆𝐈𝐍𝐆 𝐔𝐍𝐄𝐌𝐏𝐋𝐎𝐘𝐄𝐃.',
            '𝐀𝐍𝐎 𝐏𝐀 𝐇𝐈𝐍𝐈𝐇𝐈𝐍𝐓𝐀𝐘 𝐌𝐎? 𝐌𝐀𝐆-𝐀𝐏𝐏𝐋𝐘 𝐊𝐀 𝐍𝐀.',
            '𝐇𝐔𝐖𝐀𝐆 𝐌𝐎 𝐍𝐀 𝐏𝐀𝐋𝐀𝐆𝐏𝐀𝐒𝐈𝐍. 𝐀𝐏𝐏𝐋𝐘.',
            '𝐏𝐀𝐒𝐀 𝐊𝐀 𝐍𝐀 𝐍𝐆 𝐑𝐄𝐒𝐔𝐌𝐄. 𝐇𝐈𝐍𝐃𝐈 𝐈𝐓𝐎 𝐌𝐀𝐆-𝐀𝐀𝐏𝐏𝐋𝐘 𝐏𝐀𝐑𝐀 𝐒𝐀𝐘𝐎.',
            '𝐌𝐀𝐘 𝐁𝐀𝐆𝐎𝐍𝐆 𝐖𝐎𝐑𝐊. 𝐆𝐀𝐋𝐀𝐖-𝐆𝐀𝐋𝐀𝐖 𝐍𝐀.'
        ]
    def brightdata_inputs(self, kind: str) -> list[dict]:
        raw=self.brightdata_linkedin_jobs_inputs_json if kind=='linkedin_jobs' else self.brightdata_jobstreet_inputs_json
        try:
            data=json.loads(raw)
            return data if isinstance(data,list) and all(isinstance(x,dict) for x in data) else []
        except json.JSONDecodeError: return []

@lru_cache
def settings() -> Settings: return Settings()
