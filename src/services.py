from __future__ import annotations
import asyncio, logging, random, json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from .config import Settings
from .jobs import NormalizedJob, evaluate, extract_skills, is_ph_location, freshness, is_active_listing
from .models import Base, Job, JobStatus, SourceRun, SourceHealth, AppState
from .security import ActionTokens
log=logging.getLogger(__name__)
class Repository:
    def __init__(self, url: str):
        # Supabase commonly supplies postgresql:// URLs; explicitly select the
        # bundled psycopg v3 dialect instead of SQLAlchemy's psycopg2 default.
        if url.startswith('postgresql://'):
            url='postgresql+psycopg://'+url.removeprefix('postgresql://')
        elif url.startswith('postgres://'):
            url='postgresql+psycopg://'+url.removeprefix('postgres://')
        if url.startswith('sqlite:///') and not url.startswith('sqlite:////'):
            Path(url.removeprefix('sqlite:///')).parent.mkdir(parents=True, exist_ok=True)
        connect_args={'check_same_thread':False} if url.startswith('sqlite') else {}
        self.engine=create_engine(url,connect_args=connect_args); self.sessions=sessionmaker(self.engine,expire_on_commit=False)
    def create_schema(self): Base.metadata.create_all(self.engine)
    def save(self, item: NormalizedJob, score:int, reasons:list[str], warnings:list[str]) -> Job | None:
        with self.sessions() as s:
            existing=s.scalar(select(Job).where(Job.fingerprint==item.fingerprint))
            # A changed source ID plus a genuinely newer source timestamp is a
            # repost/reactivation, not a duplicate discovery of an old listing.
            if existing:
                stored_date=existing.date_posted.replace(tzinfo=timezone.utc) if existing.date_posted and existing.date_posted.tzinfo is None else existing.date_posted
                repost=(existing.source==item.source and item.source_job_id and item.source_job_id!=existing.source_job_id and item.date_posted and (not stored_date or item.date_posted>stored_date))
                if not repost: return None
                existing.source_job_id=item.source_job_id; existing.date_posted=item.date_posted; existing.url=item.url; existing.application_url=item.application_url; existing.description=item.description; existing.score=score; existing.match_reasons=reasons; existing.warnings=warnings; existing.raw_metadata={**item.raw_metadata,'reposted':True}; existing.status=JobStatus.NEW.value; existing.notification_state='PENDING'
                s.commit(); return existing
            job=Job(fingerprint=item.fingerprint,source=item.source,source_job_id=item.source_job_id,title=item.title,company=item.company,location=item.location,work_setup=item.work_setup,description=item.description,url=item.url,application_url=item.application_url,application_email=item.application_email,salary=item.salary,date_posted=item.date_posted,employment_type=item.employment_type,seniority=item.seniority,skills=extract_skills(item),raw_metadata=item.raw_metadata,score=score,match_reasons=reasons,warnings=warnings)
            s.add(job)
            try: s.commit(); return job
            except IntegrityError: s.rollback(); return None
    def run_start(self, source):
        with self.sessions() as s: x=SourceRun(source=source); s.add(x); s.commit(); return x.id
    def run_finish(self,id,**values):
        with self.sessions() as s:
            x=s.get(SourceRun,id)
            for k,v in values.items(): setattr(x,k,v)
            x.completed_at=datetime.now(timezone.utc); s.commit()
    def health(self, source):
        with self.sessions() as s:
            item=s.get(SourceHealth,source)
            if not item: item=SourceHealth(source=source); s.add(item); s.commit()
            return item
    def health_success(self, source, jobs, baseline=False):
        with self.sessions() as s:
            item=s.get(SourceHealth,source) or SourceHealth(source=source); s.add(item)
            item.last_checked_at=item.last_success_at=datetime.now(timezone.utc); item.last_job_count=jobs; item.consecutive_failures=0; item.last_error=None; item.status='healthy'; item.baseline_initialized=baseline or item.baseline_initialized; s.commit()
    def health_failure(self, source, error):
        with self.sessions() as s:
            item=s.get(SourceHealth,source) or SourceHealth(source=source); s.add(item)
            item.last_checked_at=datetime.now(timezone.utc); item.consecutive_failures+=1; item.last_error=error; item.status='unhealthy'; s.commit()
    def reserve_baseline_alert(self, limit=5):
        with self.sessions() as s:
            state=s.get(AppState,'baseline_alert_count')
            if not state: state=AppState(key='baseline_alert_count',value='0'); s.add(state)
            count=int(state.value)
            if count>=limit: s.commit(); return False
            state.value=str(count+1); s.commit(); return True
    def reserve_brightdata_page_loads(self, source: str, requested: int, limit: int) -> bool:
        """Atomically reserve a monthly Bright Data page-load budget.

        A rejected reservation happens before a paid/free-quota-consuming scrape
        request.  Each source is isolated so record-based sources do not affect
        JobStreet's page-load guard.
        """
        if requested < 1 or limit < 1:
            return False
        month=datetime.now(timezone.utc).strftime('%Y-%m')
        key=f'brightdata_page_loads:{source}:{month}'
        with self.sessions() as s:
            state=s.get(AppState,key)
            if not state:
                state=AppState(key=key,value='0'); s.add(state)
            used=int(state.value)
            if used+requested > limit:
                s.commit(); return False
            state.value=str(used+requested); s.commit(); return True
    def expire_stale_jobs(self):
        cutoff=datetime.now(timezone.utc)-timedelta(days=30)
        with self.sessions() as s:
            rows=s.query(Job).filter(Job.date_posted.is_not(None),Job.date_posted<cutoff,Job.status.notin_([JobStatus.APPLIED.value,JobStatus.OFFER.value,JobStatus.REJECTED.value])).all()
            for job in rows: job.status=JobStatus.EXPIRED.value
            s.commit(); return len(rows)
    def by_fingerprint(self, fingerprint: str) -> Job | None:
        with self.sessions() as s:
            return s.scalar(select(Job).where(Job.fingerprint==fingerprint))
    def state(self, key: str, default=None):
        with self.sessions() as s:
            item=s.get(AppState,key)
            if not item: return default
            try: return json.loads(item.value)
            except json.JSONDecodeError: return default
    def set_state(self, key: str, value) -> None:
        with self.sessions() as s:
            item=s.get(AppState,key)
            encoded=json.dumps(value,default=str)
            if item: item.value=encoded
            else: s.add(AppState(key=key,value=encoded))
            s.commit()
class Discord:
    def __init__(self,url:str|None, motivations:list[str]|str='', cfg:Settings|None=None): self.url=url; self.motivations=motivations if isinstance(motivations,list) else [motivations]; self.cfg=cfg
    def _link(self,job,action):
        if not self.cfg or not self.cfg.public_base_url: return None
        token=ActionTokens(self.cfg).issue(job.id,action)
        return f'{self.cfg.public_base_url.rstrip("/")}/actions/{token}' if token else None
    @staticmethod
    def _posted_label(value):
        if not value: return None
        today=datetime.now(ZoneInfo('Asia/Manila')).date(); posted=value.astimezone(ZoneInfo('Asia/Manila')).date(); days=(today-posted).days
        if days<=0: return 'Posted: Today'
        if days==1: return 'Posted: Yesterday'
        if days<=14: return f'Posted: {days} days ago'
        return f'Posted: {posted.strftime("%b %d, %Y")}'
    def payload(self, job: Job, test=False):
        label='𝐇𝐈𝐆𝐇 𝐌𝐀𝐓𝐂𝐇' if job.score>=85 else '𝐄𝐍𝐓𝐑𝐘-𝐋𝐄𝐕𝐄𝐋 𝐓𝐄𝐂𝐇'
        details=[job.location or 'Location not stated']
        if job.work_setup and job.work_setup.lower() not in (job.location or '').lower(): details.append(job.work_setup)
        if job.salary: details.append(job.salary)
        if job.date_posted: details.append(self._posted_label(job.date_posted))
        match='\n'.join(job.match_reasons[:6]) or 'Entry-level technology role'
        fields=[{'name':'𝐖𝐇𝐘 𝐈𝐓 𝐅𝐈𝐓𝐒','value':match,'inline':False}]
        if job.warnings: fields.append({'name':'𝐍𝐎𝐓𝐄𝐒','value':'\n'.join(job.warnings[:2]),'inline':False})
        row1=[{'type':2,'style':5,'label':'VIEW JOB','url':job.url}]
        review=self._link(job,'review')
        if review and job.status not in ('APPLIED','IGNORED'):
            row1 += [{'type':2,'style':5,'label':'APPLY NOW','url':review}]
        rows=[{'type':1,'components':row1}]
        actions=[('REVIEW APPLICATION','review'),('SAVE','saved'),('SKIP','ignored')]
        row2=[{'type':2,'style':5,'label':label,'url':url} for label,action in actions if (url:=self._link(job,action))]
        if self.cfg and self.cfg.public_base_url:
            row2.append({'type':2,'style':5,'label':'SEARCH JOBS','url':f'{self.cfg.public_base_url.rstrip("/")}/search'})
        if row2: rows.append({'type':1,'components':row2})
        kind,name=(job.source.split(':',1)+[''])[:2] if ':' in job.source else (job.source,'')
        footer=f'{kind.replace("_"," ").title()}' + (f' · {name}' if name else '')
        if test: footer='𝐓𝐄𝐒𝐓 𝐀𝐋𝐄𝐑𝐓 — No real application will be sent.'
        fields.append({'name':'𝐒𝐓𝐀𝐓𝐔𝐒','value':'Ready to review' if job.status not in ('APPLIED','IGNORED') else job.status.title(),'inline':False})
        if self.cfg and self.cfg.public_base_url:
            fields.append({'name':'𝐖𝐀𝐍𝐓 𝐓𝐎 𝐅𝐈𝐍𝐃 𝐀 𝐒𝐏𝐄𝐂𝐈𝐅𝐈𝐂 𝐉𝐎𝐁?','value':'Find or filter the exact role you want to apply for here.','inline':False})
        return {'content':random.choice(self.motivations),'embeds':[{'title':label,'description':f'**{job.score}% MATCH**\n\n**{job.title}**\n{job.company}\n\n'+' · '.join(details),'url':job.url,'fields':fields,'footer':{'text':footer}}],'components':[] if test else rows}
    async def send(self,job):
        if not self.url: return 'SKIPPED'
        return await self.send_payload(self.payload(job))
    async def send_payload(self,payload):
        if not self.url: return 'SKIPPED'
        async with httpx.AsyncClient(timeout=15) as c:
            r=await c.post(self.url,params={'with_components':'true'},json=payload); r.raise_for_status()
        return 'SENT'
    async def update_status(self, repo: Repository, scheduler_state: dict):
        """Create one permanent control panel, then edit it after each cycle."""
        if not self.url: return 'SKIPPED'
        def stamp(value):
            if not value: return '—'
            return datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Manila')).strftime('%I:%M %p')
        sources=scheduler_state.get('sources_working',0); linked=scheduler_state.get('linkedin_status','DISABLED'); jobstreet=scheduler_state.get('jobstreet_status','DISABLED'); phase=scheduler_state.get('phase','complete')
        system=f"Service\nONLINE\n\nScheduler\n{scheduler_state.get('status','RUNNING').upper()}\n\nLast scan\n{stamp(scheduler_state.get('last_poll_at'))}\n\nNext scan\n{stamp(scheduler_state.get('next_poll_at'))}\n\nNext batch\n{max(0,scheduler_state.get('seconds_until_next_poll',0))//60} minutes"
        sources_text=f"Sources\n{sources} active\n\nLinkedIn\n{linked}\n\nJobStreet\n{jobstreet}\n\nDatabase\nCONNECTED\n\nDiscord\nCONNECTED"
        scan_text='Checking recent active jobs…' if phase=='scanning' else f"Jobs checked: {scheduler_state.get('jobs_checked',0)}\nRecent PH tech jobs: {scheduler_state.get('recent_jobs_found',0)}\nNew qualifying jobs: {scheduler_state.get('new_recent_jobs',0)}\nAlerts sent: {scheduler_state.get('alerts_sent',0)}\nDuplicates ignored: {scheduler_state.get('duplicates_ignored',0)}"
        payload={'embeds':[{'title':'𝐀𝐅𝐓𝐄𝐑 𝐇𝐎𝐔𝐑𝐒 𝐉𝐎𝐁 𝐇𝐔𝐍𝐓𝐄𝐑','description':'Your automated Philippine tech-job monitor is online.','color':0xF59E0B,'fields':[{'name':'𝐒𝐘𝐒𝐓𝐄𝐌 𝐒𝐓𝐀𝐓𝐔𝐒','value':system,'inline':True},{'name':'𝐂𝐎𝐍𝐍𝐄𝐂𝐓𝐈𝐎𝐍𝐒','value':sources_text,'inline':True},{'name':'𝐒𝐂𝐀𝐍𝐍𝐈𝐍𝐆 𝐍𝐎𝐖' if phase=='scanning' else '𝐒𝐂𝐀𝐍 𝐂𝐎𝐌𝐏𝐋𝐄𝐓𝐄','value':scan_text,'inline':False},{'name':'𝐇𝐎𝐖 𝐓𝐎 𝐔𝐒𝐄','value':'Auto scanning runs every 15 minutes. Use SEARCH JOBS for a specific role, or SCAN NOW for one protected immediate batch.','inline':False}],'footer':{'text':'After Hours Job Hunter • Made by masoncalix'}}]}
        if self.cfg and self.cfg.public_base_url:
            base=self.cfg.public_base_url.rstrip('/'); scan=ActionTokens(self.cfg).issue_control('scan')
            row1=[{'type':2,'style':5,'label':'SEARCH JOBS','url':f'{base}/search'}]
            if scan: row1.insert(0,{'type':2,'style':5,'label':'SCAN NOW','url':f'{base}/control/scan/{scan}'})
            payload['components']=[{'type':1,'components':row1},{'type':1,'components':[{'type':2,'style':5,'label':'VIEW STATUS','url':f'{base}/status'},{'type':2,'style':5,'label':'VIEW LATEST JOBS','url':f'{base}/latest-page'}]},{'type':1,'components':[{'type':2,'style':5,'label':'HELP / HOW TO USE','url':f'{base}/help'}]}]
        existing=repo.state('discord_control_panel_message_id') or repo.state('discord_status_message_id')
        async with httpx.AsyncClient(timeout=15) as client:
            if existing:
                response=await client.patch(f'{self.url}/messages/{existing}',json=payload)
                if response.status_code==404: existing=None
                else: response.raise_for_status(); return 'UPDATED'
            response=await client.post(self.url,params={'wait':'true'},json=payload); response.raise_for_status()
            message_id=response.json().get('id')
            if message_id: repo.set_state('discord_control_panel_message_id',message_id)
        return 'CREATED'
class Pipeline:
    def __init__(self, repo:Repository, config:Settings): self.repo=repo; self.config=config; self.discord=Discord(config.discord_webhook_url,config.discord_motivations,config); self._baseline_lock=asyncio.Lock(); self._cycle_lock=asyncio.Lock(); self._cycle_notifications=0
    def begin_cycle(self): self._cycle_notifications=0
    async def process(self, item:NormalizedJob, notify=True) -> tuple[Job|None,bool]:
        if not is_ph_location(item) or not is_active_listing(item): return None, False
        score,reasons,warnings,relevant=evaluate(item)
        _,_,fresh=freshness(item)
        if not relevant or not fresh or (freshness(item)[0] < 0 and score < 85): return None, False
        job=self.repo.save(item,score,reasons,warnings)
        if not job: return None, True
        if notify and score>=self.config.min_notify_score: await self.notify(job)
        return job, True
    async def notify(self, job: Job):
        async with self._cycle_lock:
            if self._cycle_notifications>=self.config.max_notifications_per_cycle: return
            self._cycle_notifications+=1
        try:
            job.notification_state=await self.discord.send(job)
            if job.notification_state=='SENT': job.status=JobStatus.NOTIFIED.value
        except httpx.HTTPError as e:
            job.notification_state='FAILED'; log.warning('discord_notification_failed',extra={'job_id':job.id,'error':str(e)})
        with self.repo.sessions() as s:
            stored=s.get(Job,job.id); stored.notification_state=job.notification_state; stored.status=job.status; s.commit()
    async def retry_notifications(self):
        if not self.config.discord_webhook_url: return 0
        with self.repo.sessions() as s:
            ids=s.scalars(select(Job.id).where(Job.notification_state=='FAILED',Job.score>=self.config.min_notify_score)).all()
        for job_id in ids:
            with self.repo.sessions() as s: job=s.get(Job,job_id); await self.notify(job)
        return len(ids)
    async def run_source(self,source):
        run=self.repo.run_start(source.name); discovered=new=filtered=0
        try:
            for attempt in range(3):
                try: items=await source.fetch(); break
                except Exception:
                    if attempt==2: raise
                    await asyncio.sleep(2**attempt)
            baseline=not self.repo.health(source.name).baseline_initialized
            ordered=sorted(items,key=lambda x:evaluate(x)[0],reverse=True)
            baseline_candidates=[]
            for item in ordered:
                discovered+=1; job,accepted=await self.process(item,notify=not baseline); filtered+=not accepted; new+=job is not None
                if baseline and job and job.score>=self.config.min_notify_score: baseline_candidates.append(job)
            if baseline:
                for job in baseline_candidates:
                    async with self._baseline_lock:
                        allowed=self.repo.reserve_baseline_alert()
                    if not allowed: break
                    await self.notify(job)
            self.repo.run_finish(run,success=True,discovered=discovered,new_jobs=new,filtered=filtered)
            self.repo.health_success(source.name,discovered,baseline=True)
        except Exception as e:
            self.repo.run_finish(run,success=False,error=str(e)); log.warning('source_failed',extra={'source':source.name,'error':str(e)})
            self.repo.health_failure(source.name,str(e))
        return {'source':source.name,'discovered':discovered,'new':new,'filtered':filtered}
