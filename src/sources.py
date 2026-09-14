from __future__ import annotations
import asyncio, logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
import httpx
from .jobs import NormalizedJob
log=logging.getLogger(__name__)
class SourceError(Exception): pass
class Source(ABC):
    name: str
    @abstractmethod
    async def fetch(self) -> list[NormalizedJob]: ...
class DisabledBoard(Source):
    def __init__(self,name:str): self.name=name
    async def fetch(self): raise SourceError("No official public endpoint configured; adapter disabled")
class Greenhouse(Source):
    def __init__(self,target): self.name=f"greenhouse:{target['name']}"; self.token=target['token']
    async def fetch(self):
        data=await get_json(f"https://boards-api.greenhouse.io/v1/boards/{self.token}/jobs?content=true")
        return [NormalizedJob(self.name,x['title'],x.get('departments',[{}])[0].get('name','Unknown'),x.get('location',{}).get('name',''),x.get('content',''),x['absolute_url'],str(x['id']),x['absolute_url'],date_posted=parse_date(x.get('updated_at')),raw_metadata={'ats':'greenhouse'}) for x in data.get('jobs',[])]
class Lever(Source):
    def __init__(self,target): self.name=f"lever:{target['name']}"; self.site=target.get('token') or target['site']
    async def fetch(self):
        data=await get_json(f"https://api.lever.co/v0/postings/{self.site}?mode=json")
        return [NormalizedJob(self.name,x['text'],x.get('categories',{}).get('team') or self.site,x.get('categories',{}).get('location',''),x.get('descriptionPlain',''),x['hostedUrl'],x.get('id'),x['hostedUrl'],date_posted=datetime.fromtimestamp(x['createdAt']/1000,tz=timezone.utc),raw_metadata={'ats':'lever'}) for x in data]
class Ashby(Source):
    def __init__(self,target): self.name=f"ashby:{target['name']}"; self.board=target.get('token') or target['board']
    async def fetch(self):
        data=await get_json(f"https://api.ashbyhq.com/posting-api/job-board/{self.board}")
        return [NormalizedJob(self.name,x['title'],self.board,x.get('location',''),x.get('descriptionPlain',''),x['jobUrl'],x.get('id'),x['jobUrl'],date_posted=parse_date(x.get('publishedAt')),raw_metadata={'ats':'ashby'}) for x in data.get('jobs',[]) if x.get('isListed',True)]
async def get_json(url):
    try:
        async with httpx.AsyncClient(timeout=20,follow_redirects=True) as c:
            r=await c.get(url,headers={'User-Agent':'PH-Job-Agent/1.0 contact: self-hosted'}); r.raise_for_status(); return r.json()
    except (httpx.HTTPError, ValueError) as e: raise SourceError(str(e)) from e
def parse_date(value):
    if not value: return None
    try: return datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError: return None
def configured_sources(targets):
    factories={'greenhouse':Greenhouse,'lever':Lever,'ashby':Ashby}; result=[]
    for t in targets:
        if t.get('kind') in factories:
            try: result.append(factories[t['kind']](t))
            except KeyError: log.warning('invalid_source_target',extra={'target':t})
    if not result: log.warning('no_public_ats_sources_configured')
    return result
