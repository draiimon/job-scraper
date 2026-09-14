"""Provider-neutral preparation for future Gmail alert ingestion; no OAuth required."""
from __future__ import annotations
from datetime import datetime
from .jobs import NormalizedJob
def alert_to_job(kind:str, title:str, company:str, location:str, url:str, received_at:datetime, snippet:str='') -> NormalizedJob:
    if kind not in {'linkedin_alert','jobstreet_alert'}: raise ValueError('unsupported alert source')
    return NormalizedJob(source=kind,title=title,company=company,location=location,description=snippet,url=url,application_url=url,date_posted=received_at,raw_metadata={'ingested_from':'official_email_alert'})
