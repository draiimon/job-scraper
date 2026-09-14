from __future__ import annotations
import asyncio, hashlib, json, logging, random
from datetime import datetime
import httpx
from .config import Settings
from .jobs import NormalizedJob
from .sources import Source, SourceError, parse_date
log=logging.getLogger(__name__)
class BrightDataClient:
    base_url='https://api.brightdata.com/datasets/v3/scrape'
    def __init__(self, token:str, retries:int=3, timeout:float=30):
        self._token=token; self.retries=retries; self.timeout=timeout; self._cache={}
    async def list_datasets(self):
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response=await client.get('https://api.brightdata.com/datasets/list',headers={'Authorization':f'Bearer {self._token}'})
            if response.status_code in (401,403): raise SourceError(f'Bright Data authentication rejected ({response.status_code})')
            response.raise_for_status()
            data=response.json()
            if not isinstance(data,list): raise SourceError('Bright Data returned an unsupported dataset catalog')
            return data
        except httpx.HTTPError as exc:
            raise SourceError('Bright Data catalog request failed') from exc
    async def scrape(self,dataset_id:str,input_rows:list[dict],limit_per_input=None,extra_params:dict|None=None):
        if not dataset_id or not input_rows: raise SourceError('Bright Data dataset ID and input rows are required')
        payload={'input':input_rows,'limit_per_input':limit_per_input}; key=hashlib.sha256((dataset_id+json.dumps(payload,sort_keys=True)).encode()).hexdigest()
        if key in self._cache: return self._cache[key]
        for attempt in range(self.retries+1):
            try:
                params={'dataset_id':dataset_id,'notify':'false','include_errors':'true',**(extra_params or {})}
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response=await client.post(self.base_url,params=params,headers={'Authorization':f'Bearer {self._token}','Content-Type':'application/json'},json=payload)
                if response.is_success:
                    data=response.json(); self._cache[key]=data; return data
                if response.status_code in (401,403): raise SourceError(f'Bright Data authentication rejected ({response.status_code})')
                if response.status_code==429 or response.status_code in (408,500,502,503,504):
                    if attempt==self.retries: raise SourceError(f'Bright Data temporarily unavailable ({response.status_code})')
                    retry_after=response.headers.get('Retry-After')
                    delay=float(retry_after) if retry_after and retry_after.replace('.','',1).isdigit() else min(30,2**attempt+random.uniform(0,1))
                    await asyncio.sleep(delay); continue
                raise SourceError(f'Bright Data request failed ({response.status_code})')
            except httpx.HTTPError as exc:
                if attempt==self.retries: raise SourceError('Bright Data network failure') from exc
                await asyncio.sleep(min(30,2**attempt+random.uniform(0,1)))
        raise SourceError('Bright Data request failed')
class BrightDataJobs(Source):
    def __init__(self,kind:str,dataset_id:str,inputs:list[dict],client:BrightDataClient):
        self.kind=kind; self.name=f'brightdata:{kind}'; self.dataset_id=dataset_id; self.inputs=inputs; self.client=client
    async def fetch(self):
        discovery={'type':'discover_new','discover_by':'keyword'} if self.kind=='linkedin_jobs' else None
        data=await self.client.scrape(self.dataset_id,self.inputs,extra_params=discovery)
        rows=data if isinstance(data,list) else data.get('data',data.get('results',[]))
        if not isinstance(rows,list): raise SourceError('Bright Data returned an unsupported response shape')
        return [self._normalize(row) for row in rows if isinstance(row,dict) and self._usable(row)]
    def _usable(self,row):
        return bool(self._pick(row,'title','job_title','position') and self._pick(row,'url','job_url','apply_url'))
    def _pick(self,row,*keys):
        for key in keys:
            if row.get(key): return str(row[key])
        return ''
    def _normalize(self,row):
        return NormalizedJob(source=self.name,source_job_id=self._pick(row,'id','job_id'),title=self._pick(row,'title','job_title','position'),company=self._pick(row,'company','company_name','employer') or 'Unknown company',location=self._pick(row,'location','job_location'),description=self._pick(row,'description','job_description','snippet'),url=self._pick(row,'url','job_url','apply_url'),application_url=self._pick(row,'apply_url','application_url','url','job_url'),date_posted=parse_date(self._pick(row,'date_posted','posted_at','publication_date')),employment_type=self._pick(row,'employment_type','job_type') or None,raw_metadata={'provider':'brightdata','dataset_kind':self.kind})
def brightdata_sources(cfg:Settings):
    if not cfg.brightdata_enabled or not cfg.brightdata_api_token: return []
    client=BrightDataClient(cfg.brightdata_api_token); result=[]
    for kind,dataset in (('linkedin_jobs',cfg.brightdata_linkedin_jobs_dataset_id),('jobstreet',cfg.brightdata_jobstreet_dataset_id)):
        inputs=cfg.brightdata_inputs(kind)
        if dataset and inputs: result.append(BrightDataJobs(kind,dataset,inputs,client))
    return result
