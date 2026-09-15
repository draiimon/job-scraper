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


@dataclass(frozen=True)
class RevisionOutcome:
    """A safe per-request result; it never contains an API key or raw error."""
    text: str | None
    failure_reason: str | None = None
    cache_hit: bool = False


class GeminiManager:
    """Free-tier-aware Gemini caller. Keys share 'default' unless mapped to distinct authorized projects."""
    def __init__(self, cfg:Settings):
        projects=cfg.gemini_projects
        self.cfg=cfg; self.keys=[KeyState(i,k,projects.get(i,'default')) for i,k in cfg.gemini_keys]
        self.cache={}; self.requests_today=0; self.usage_day=date.today(); self.cache_hits=0; self.last_429=None; self._queue=asyncio.Queue(); self._worker=None; self._sem=asyncio.Semaphore(cfg.gemini_concurrency_limit)
    def _rollover(self):
        if self.usage_day != date.today(): self.usage_day=date.today(); self.requests_today=0
    def health(self):
        self._rollover(); now=time.time(); enabled=self.cfg.ai_enabled and self.cfg.cover_letter_mode=='ai'
        active=[x for x in self.keys if not x.disabled and not x.daily_exhausted and x.cooldown_until<=now]
        if self.requests_today>=self.cfg.ai_daily_request_limit: active=[]
        if not enabled:
            status='DISABLED'
        elif self.requests_today>=self.cfg.ai_daily_request_limit:
            status='DAILY_LIMIT'
        elif active:
            status='READY'
        elif any(key.daily_exhausted for key in self.keys):
            status='QUOTA_EXHAUSTED'
        elif any(key.cooldown_until>now for key in self.keys):
            status='RATE_LIMITED'
        elif not self.keys:
            status='NO_API_KEY'
        else:
            status='UNAVAILABLE'
        return {'status':status,'provider':'GEMINI','available':bool(enabled and active),'enabled':enabled,'mode':self.cfg.cover_letter_mode,'requests_today':self.requests_today,'cache_hits':self.cache_hits,'active_cooldowns':sum(x.cooldown_until>now for x in self.keys),'last_429':self.last_429,'model':self.cfg.gemini_model}
    async def revise(self, prompt:str) -> str | None:
        """Backward-compatible text-only API for callers that do not need status."""
        return (await self.revise_with_status(prompt)).text

    async def revise_with_status(self, prompt: str) -> RevisionOutcome:
        """Return a per-generation outcome so the UI never lies about fallback."""
        if not self.cfg.ai_enabled or self.cfg.cover_letter_mode!='ai':
            return RevisionOutcome(None, 'AI_DISABLED')
        if not self.keys:
            return RevisionOutcome(None, 'NO_API_KEY')
        self._rollover()
        if self.requests_today>=self.cfg.ai_daily_request_limit:
            return RevisionOutcome(None, 'DAILY_LIMIT')
        digest=hashlib.sha256(prompt.encode()).hexdigest()
        if digest in self.cache:
            self.cache_hits+=1
            return RevisionOutcome(self.cache[digest], cache_hit=True)
        future=asyncio.get_running_loop().create_future(); await self._queue.put((digest,prompt,future))
        if not self._worker or self._worker.done(): self._worker=asyncio.create_task(self._work())
        try: return await future
        except asyncio.CancelledError: raise
        except Exception: return RevisionOutcome(None, 'AI_UNAVAILABLE')
    async def _work(self):
        while not self._queue.empty():
            digest,prompt,future=await self._queue.get()
            try:
                async with self._sem:
                    answer, reason=await asyncio.wait_for(
                        self._request_with_reason(prompt),
                        timeout=max(1, self.cfg.gemini_total_timeout_seconds),
                    )
                if answer: self.cache[digest]=answer
                if not future.done(): future.set_result(RevisionOutcome(answer, reason))
            except asyncio.TimeoutError:
                if not future.done(): future.set_result(RevisionOutcome(None, 'TIMEOUT'))
            except Exception:
                if not future.done(): future.set_result(RevisionOutcome(None, 'AI_UNAVAILABLE'))
            finally: self._queue.task_done()
    def _candidate(self):
        self._rollover(); now=time.time()
        if self.requests_today>=self.cfg.ai_daily_request_limit: return []
        available=[x for x in self.keys if not x.disabled and not x.daily_exhausted and x.cooldown_until<=now]
        # One key per pool per attempt: no rotation within a project's quota pool.
        pools={}; [pools.setdefault(x.pool,x) for x in available]
        return list(pools.values())
    async def _request(self, prompt):
        """Compatibility wrapper retained for existing rate-limit tests."""
        return (await self._request_with_reason(prompt))[0]

    async def _request_with_reason(self, prompt):
        last_reason = 'AI_UNAVAILABLE'
        for attempt in range(self.cfg.gemini_max_retries+1):
            candidates=self._candidate()
            if not candidates:
                if self.requests_today>=self.cfg.ai_daily_request_limit:
                    return None, 'DAILY_LIMIT'
                if any(key.daily_exhausted for key in self.keys):
                    return None, 'QUOTA_EXHAUSTED'
                if any(key.cooldown_until > time.time() for key in self.keys):
                    return None, 'RATE_LIMIT'
                return None, last_reason
            retry=False; server_delay=0.0
            for key in candidates:
                result,status,delay,body=await self._post(key,prompt)
                if result: return result, None
                if status in (400,401) or (status==403 and 'quota' not in body.lower()):
                    key.disabled=True; last_reason='INVALID_KEY'; log.warning('gemini_key_disabled key=%s status=%s',key.masked,status); continue
                if 'quota' in body.lower() and ('daily' in body.lower() or 'project' in body.lower()):
                    for other in self.keys:
                        if other.pool==key.pool: other.daily_exhausted=True
                    last_reason='QUOTA_EXHAUSTED'; log.warning('gemini_pool_daily_exhausted pool=%s',key.pool); continue
                if status==429 or status in (408,500,502,503,504):
                    self.last_429=datetime.now(timezone.utc).isoformat() if status==429 else self.last_429
                    last_reason='RATE_LIMIT' if status==429 else ('TIMEOUT' if status==408 else 'TRANSIENT_ERROR')
                    wait=delay if delay is not None else min(60,(2**attempt)+random.uniform(0,1))
                    server_delay=max(server_delay,wait)
                    for other in self.keys:
                        if other.pool==key.pool: other.cooldown_until=max(other.cooldown_until,time.time()+wait)
                    log.warning('gemini_cooldown key=%s seconds=%d',key.masked,wait); retry=True
                else:
                    last_reason='INVALID_RESPONSE'
            if not retry: return None, last_reason
            if attempt >= self.cfg.gemini_max_retries: return None, last_reason
            await asyncio.sleep(max(server_delay,min(60,(2**attempt)+random.uniform(0,1))))
        return None, last_reason
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
