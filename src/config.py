from __future__ import annotations
import json
import os
from pathlib import Path
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import dotenv_values

class Settings(BaseSettings):
    # Render/Replit may expose optional settings as empty environment
    # variables.  Empty values must behave like unset values so defaults such
    # as the 15-minute scheduler interval remain active.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)
    database_url: str = "sqlite:///./data/job_agent.sqlite3"
    discord_webhook_url: str | None = None
    discord_bot_token: str | None = None
    discord_bot_guild_id: str | None = None
    discord_control_channel_id: str | None = None
    discord_motivation: str = ""
    discord_motivations_json: str = ""
    min_notify_score: int = 70
    min_auto_application_score: int = 85
    max_notifications_per_cycle: int = 3
    manual_scan_cooldown_seconds: int = 300
    scan_source_concurrency: int = 6
    scan_source_timeout_seconds: int = 15
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
    resume_path: str | None = None
    application_dry_run: bool = True
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
    # Bright Data's documented LinkedIn keyword-discovery dataset. It is only
    # called when BRIGHTDATA_ENABLED=true and a token is configured.
    brightdata_linkedin_jobs_dataset_id: str | None = "gd_lpfll7v5hcqtkxl6l"
    brightdata_jobstreet_dataset_id: str | None = None
    brightdata_linkedin_jobs_inputs_json: str = '[]'
    brightdata_jobstreet_inputs_json: str = '[]'
    # JobStreet is billed/limited by page load.  Keep this deliberately below
    # the advertised free allowance unless the operator explicitly changes it.
    brightdata_jobstreet_monthly_page_limit: int = 250
    brightdata_linkedin_monthly_request_limit: int = 100
    jobstreet_session_path: str = "data/private/jobstreet_session.json"
    jobstreet_base_url: str = "https://ph.jobstreet.com"
    jobstreet_login_url: str = ""
    jobstreet_location: str = "Philippines"
    jobstreet_search_terms_json: str = '["DevOps", "Cloud", "IT Support"]'
    jobstreet_max_results: int = 50
    jobstreet_auth_timeout_seconds: int = 600
    jobstreet_scan_timeout_seconds: int = 60

    @property
    def source_targets(self) -> list[dict]:
        raw=self.source_targets_json.strip()
        source_name='SOURCE_TARGETS_JSON'
        if not raw:
            source_name=self.source_config_path
            try:
                raw=Path(self.source_config_path).read_text(encoding='utf-8')
            except OSError as exc:
                raise ValueError(f'Unable to read source configuration: {self.source_config_path}') from exc
        try:
            parsed=json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f'{source_name} must contain a JSON array') from exc
        if not isinstance(parsed,list) or not parsed:
            raise ValueError(f'{source_name} must contain at least one source target')
        if not all(isinstance(target,dict) for target in parsed):
            raise ValueError(f'{source_name} must contain only JSON objects')
        return parsed
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
        if self.discord_motivation.strip(): return [self.discord_motivation.strip()]
        return [
            '𝐏𝐔𝐓𝐀𝐍𝐆 𝐈𝐍𝐀 𝐌𝐎! 𝐌𝐀𝐆-𝐀𝐏𝐏𝐋𝐘 𝐊𝐀 𝐍𝐀 𝐍𝐆 𝐖𝐎𝐑𝐊 𝐊𝐔𝐍𝐆 𝐀𝐘𝐀𝐖 𝐌𝐎 𝐌𝐀𝐆𝐈𝐍𝐆 𝐔𝐍𝐄𝐌𝐏𝐋𝐎𝐘𝐄𝐃.',
            '𝐀𝐍𝐎 𝐏𝐀 𝐇𝐈𝐍𝐈𝐇𝐈𝐍𝐓𝐀𝐘 𝐌𝐎? 𝐌𝐀𝐆-𝐀𝐏𝐏𝐋𝐘 𝐊𝐀 𝐍𝐀.',
            '𝐇𝐔𝐖𝐀𝐆 𝐌𝐎 𝐍𝐀 𝐏𝐀𝐋𝐀𝐆𝐏𝐀𝐒𝐈𝐍. 𝐀𝐏𝐏𝐋𝐘.',
            '𝐏𝐀𝐒𝐀 𝐊𝐀 𝐍𝐀 𝐍𝐆 𝐑𝐄𝐒𝐔𝐌𝐄. 𝐇𝐈𝐍𝐃𝐈 𝐈𝐓𝐎 𝐌𝐀𝐆-𝐀𝐀𝐏𝐏𝐋𝐘 𝐏𝐀𝐑𝐀 𝐒𝐀𝐘𝐎.',
            '𝐌𝐀𝐘 𝐁𝐀𝐆𝐎𝐍𝐆 𝐖𝐎𝐑𝐊. 𝐆𝐀𝐋𝐀𝐖-𝐆𝐀𝐋𝐀𝐖 𝐍𝐀.'
        ]
    def brightdata_inputs(self, kind: str) -> list[dict]:
        normalized='linkedin_jobs' if kind in ('linkedin','linkedin_jobs') else 'jobstreet'
        raw=self.brightdata_linkedin_jobs_inputs_json if normalized=='linkedin_jobs' else self.brightdata_jobstreet_inputs_json
        try:
            data=json.loads(raw)
            return data if isinstance(data,list) and all(isinstance(x,dict) for x in data) else []
        except json.JSONDecodeError: return []
    @property
    def jobstreet_search_terms(self) -> list[str]:
        try:
            values=json.loads(self.jobstreet_search_terms_json)
            if isinstance(values,list):
                return [str(value).strip() for value in values if str(value).strip()]
        except json.JSONDecodeError:
            pass
        return ["DevOps", "Cloud", "IT Support"]

@lru_cache
def settings() -> Settings: return Settings()
