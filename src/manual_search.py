from __future__ import annotations
import time
from datetime import datetime, timedelta, timezone
from .brightdata import BrightDataClient, BrightDataJobs
from .config import Settings
from .jobs import evaluate, freshness, is_ph_location
from .sources import SourceError

class ManualJobSearch:
    """Free-tier bounded, cached Bright Data LinkedIn discovery for /find."""
    def __init__(self, cfg: Settings, repo):
        self.cfg=cfg; self.repo=repo; self.client=BrightDataClient(cfg.brightdata_api_token or ''); self.cache={}
    @staticmethod
    def _max_age(freshness_text: str) -> int:
        text=(freshness_text or 'Past 24 hours').lower()
        if '24' in text: return 1
        if '14' in text: return 14
        if '7' in text or 'week' in text: return 7
        return 3
    async def find(self, role: str, location='Philippines', freshness_text='Past 24 hours', remote='', limit=10, work_setup='', min_score=0, entry_level_only=True, source_filter='all'):
        # This is an explicit user-initiated search, so it may use the configured
        # token even when automatic Bright Data polling is disabled.
        if not self.cfg.brightdata_api_token: raise SourceError('Bright Data LinkedIn search is not configured')
        limit=max(1,min(int(limit),10)); age=self._max_age(freshness_text)
        key=(role.strip().lower(),location.strip().lower(),freshness_text.strip().lower(),remote.strip().lower(),limit,work_setup.lower(),min_score,entry_level_only,source_filter.lower())
        cached=self.cache.get(key)
        if cached and cached[0]>time.time(): return cached[1]
        if not self.repo.reserve_brightdata_page_loads('linkedin_jobs',1,self.cfg.brightdata_linkedin_monthly_request_limit): raise SourceError('Bright Data LinkedIn monthly safety limit reached')
        if source_filter.lower() not in ('all','linkedin','brightdata_linkedin'): return []
        payload={'location':location,'keyword':role,'country':'PH' if 'philipp' in location.lower() else '','time_range':freshness_text or 'Past 24 hours','job_type':'','experience_level':'Entry level' if entry_level_only else '','remote':remote or work_setup,'company':'','selective_search':False,'jobs_to_not_include':[],'location_radius':''}
        rows=await self.client.linkedin_discovery([payload],limit)
        source=BrightDataJobs('linkedin_jobs','gd_lpfll7v5hcqtkxl6l',[payload],self.client)
        cutoff=datetime.now(timezone.utc)-timedelta(days=age)
        jobs=[]
        for row in rows if isinstance(rows,list) else []:
            if not isinstance(row,dict) or not source._usable(row): continue
            item=source._normalize(row); score,reasons,warnings,relevant=evaluate(item)
            text=f'{item.title} {item.description}'.lower()
            if not is_ph_location(item) or not item.date_posted or item.date_posted<cutoff or not freshness(item)[2] or not relevant or score<min_score: continue
            if entry_level_only and not any(x in text for x in ('junior','entry','fresh graduate','graduate','associate','trainee','0-','1 year')): continue
            if work_setup and work_setup.lower() not in text and work_setup.lower() not in item.location.lower(): continue
            stored=self.repo.save(item,score,reasons,warnings) or self.repo.by_fingerprint(item.fingerprint)
            if stored: jobs.append(stored)
        jobs.sort(key=lambda x:x.date_posted or datetime.min.replace(tzinfo=timezone.utc),reverse=True)
        self.cache[key]=(time.time()+900,jobs)
        return jobs
