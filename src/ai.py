from __future__ import annotations
import asyncio, hashlib, logging, random, time
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone
import httpx
from .config import Settings, settings
log=logging.getLogger(__name__)
@dataclass
class KeyState:
    index:str; value:str; pool:str; disabled:bool=False; cooldown_until:float=0; daily_exhausted:bool=False
    @property
    def masked(self): return '****'+self.value[-4:]
class GeminiManager:
    """Free-tier-aware Gemini caller. Keys share 'default' unless mapped to distinct authorized projects."""
    def __init__(self, cfg:Settings):
        projects=cfg.gemini_projects
        self.cfg=cfg; self.keys=[KeyState(i,k,projects.get(i,'default')) for i,k in cfg.gemini_keys]
        self.cache={}; self.requests_today=0; self.usage_day=date.today(); self.cache_hits=0; self.last_429=None; self._queue=asyncio.Queue(); self._worker=None; self._sem=asyncio.Semaphore(cfg.gemini_concurrency_limit)
    def _rollover(self):
        if self.usage_day != date.today(): self.usage_day=date.today(); self.requests_today=0
    def health(self):
        self._rollover(); now=time.time(); active=[x for x in self.keys if not x.disabled and not x.daily_exhausted and x.cooldown_until<=now]
        if self.requests_today>=self.cfg.ai_daily_request_limit: active=[]
        return {'status':'READY' if active else 'UNAVAILABLE','provider':'GEMINI','available':bool(active),'requests_today':self.requests_today,'cache_hits':self.cache_hits,'active_cooldowns':sum(x.cooldown_until>now for x in self.keys),'last_429':self.last_429,'model':self.cfg.gemini_model}
    async def revise(self, prompt:str) -> str | None:
        if not self.cfg.ai_enabled or self.cfg.cover_letter_mode!='ai' or not self.keys: return None
        self._rollover()
        if self.requests_today>=self.cfg.ai_daily_request_limit: return None
        digest=hashlib.sha256(prompt.encode()).hexdigest()
        if digest in self.cache: self.cache_hits+=1; return self.cache[digest]
        future=asyncio.get_running_loop().create_future(); await self._queue.put((digest,prompt,future))
        if not self._worker or self._worker.done(): self._worker=asyncio.create_task(self._work())
        try: return await future
        except Exception: return None
    async def _work(self):
        while not self._queue.empty():
            digest,prompt,future=await self._queue.get()
            try:
                async with self._sem:
                    answer=await asyncio.wait_for(
                        self._request(prompt),
                        timeout=max(1, self.cfg.gemini_total_timeout_seconds),
                    )
                if answer: self.cache[digest]=answer
                if not future.done(): future.set_result(answer)
            except Exception:
                if not future.done(): future.set_result(None)
            finally: self._queue.task_done()
    def _candidate(self):
        self._rollover(); now=time.time()
        if self.requests_today>=self.cfg.ai_daily_request_limit: return []
        available=[x for x in self.keys if not x.disabled and not x.daily_exhausted and x.cooldown_until<=now]
        # One key per pool per attempt: no rotation within a project's quota pool.
        pools={}; [pools.setdefault(x.pool,x) for x in available]
        return list(pools.values())
    async def _request(self,prompt):
        for attempt in range(self.cfg.gemini_max_retries+1):
            candidates=self._candidate()
            if not candidates: return None
            retry=False; server_delay=0.0
            for key in candidates:
                result,status,delay,body=await self._post(key,prompt)
                if result: return result
                if status in (400,401) or (status==403 and 'quota' not in body.lower()):
                    key.disabled=True; log.warning('gemini_key_disabled key=%s status=%s',key.masked,status); continue
                if 'quota' in body.lower() and ('daily' in body.lower() or 'project' in body.lower()):
                    for other in self.keys:
                        if other.pool==key.pool: other.daily_exhausted=True
                    log.warning('gemini_pool_daily_exhausted pool=%s',key.pool); continue
                if status==429 or status in (408,500,502,503,504):
                    self.last_429=datetime.now(timezone.utc).isoformat() if status==429 else self.last_429
                    wait=delay if delay is not None else min(60,(2**attempt)+random.uniform(0,1))
                    server_delay=max(server_delay,wait)
                    for other in self.keys:
                        if other.pool==key.pool: other.cooldown_until=max(other.cooldown_until,time.time()+wait)
                    log.warning('gemini_cooldown key=%s seconds=%d',key.masked,wait); retry=True
            if not retry: return None
            if attempt >= self.cfg.gemini_max_retries: return None
            await asyncio.sleep(max(server_delay,min(60,(2**attempt)+random.uniform(0,1))))
        return None
    async def _post(self,key,prompt):
        url=f'https://generativelanguage.googleapis.com/v1beta/models/{self.cfg.gemini_model}:generateContent'
        try:
            self.requests_today+=1
            async with httpx.AsyncClient(timeout=max(1, self.cfg.gemini_request_timeout_seconds)) as client:
                response=await client.post(url,headers={'x-goog-api-key':key.value},json={'contents':[{'parts':[{'text':prompt}]}]})
            if response.is_success:
                text=response.json()['candidates'][0]['content']['parts'][0]['text']
                return text,response.status_code,None,''
            retry_after=response.headers.get('Retry-After'); delay=float(retry_after) if retry_after and retry_after.replace('.','',1).isdigit() else None
            return None,response.status_code,delay,response.text[:1000]
        except httpx.HTTPError: return None,503,None,'transport error'
_manager: GeminiManager | None=None
def gemini() -> GeminiManager:
    global _manager
    if _manager is None: _manager=GeminiManager(settings())
    return _manager
