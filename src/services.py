from __future__ import annotations
import asyncio, logging, random
from pathlib import Path
from datetime import datetime, timezone
import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from .config import Settings
from .jobs import NormalizedJob, evaluate, extract_skills, is_ph_location
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
            if s.scalar(select(Job).where(Job.fingerprint==item.fingerprint)): return None
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
class Discord:
    def __init__(self,url:str|None, motivations:list[str]|str='', cfg:Settings|None=None): self.url=url; self.motivations=motivations if isinstance(motivations,list) else [motivations]; self.cfg=cfg
    def _link(self,job,action):
        if not self.cfg or not self.cfg.public_base_url: return None
        token=ActionTokens(self.cfg).issue(job.id,action)
        return f'{self.cfg.public_base_url.rstrip("/")}/actions/{token}' if token else None
    def payload(self, job: Job, test=False):
        label='High match' if job.score>=85 else 'New entry-level tech job'
        details=[job.location or 'Location not stated']
        if job.work_setup and job.work_setup.lower() not in (job.location or '').lower(): details.append(job.work_setup)
        if job.salary: details.append(job.salary)
        if job.date_posted: details.append(f'Posted {job.date_posted.date().isoformat()}')
        match='\n'.join(f'• {x}' for x in job.match_reasons[:5]) or '• Entry-level technology role'
        fields=[{'name':'Why it matches','value':match,'inline':False}]
        if job.warnings: fields.append({'name':'Notes','value':'\n'.join(f'• {x}' for x in job.warnings[:2]),'inline':False})
        row1=[{'type':2,'style':5,'label':'VIEW JOB','url':job.url}]
        review=self._link(job,'review')
        if review and job.status not in ('APPLIED','IGNORED'):
            row1 += [{'type':2,'style':5,'label':'APPLY NOW','url':review},{'type':2,'style':5,'label':'REVIEW APPLICATION','url':review}]
        rows=[{'type':1,'components':row1}]
        actions=[('GENERATE COVER LETTER','generate'),('MARK APPLIED','applied'),('SAVE','saved'),('SKIP','ignored'),('REMIND ME','remind')]
        row2=[{'type':2,'style':5,'label':label,'url':url} for label,action in actions if (url:=self._link(job,action))]
        if row2: rows.append({'type':1,'components':row2})
        footer=f'{job.source} · Ready to apply'
        if test: footer='𝐓𝐄𝐒𝐓 𝐀𝐋𝐄𝐑𝐓 — No real application will be sent.'
        return {'content':random.choice(self.motivations),'embeds':[{'title':f'🔔 {label} · {job.score}%','description':f'**{job.title}**\n{job.company}\n\n'+' · '.join(details),'url':job.url,'fields':fields,'footer':{'text':footer}}],'components':[] if test else rows}
    async def send(self,job):
        if not self.url: return 'SKIPPED'
        return await self.send_payload(self.payload(job))
    async def send_payload(self,payload):
        if not self.url: return 'SKIPPED'
        async with httpx.AsyncClient(timeout=15) as c:
            r=await c.post(self.url,params={'with_components':'true'},json=payload); r.raise_for_status()
        return 'SENT'
class Pipeline:
    def __init__(self, repo:Repository, config:Settings): self.repo=repo; self.config=config; self.discord=Discord(config.discord_webhook_url,config.discord_motivations,config); self._baseline_lock=asyncio.Lock()
    async def process(self, item:NormalizedJob, notify=True) -> tuple[Job|None,bool]:
        if not is_ph_location(item): return None, False
        score,reasons,warnings,relevant=evaluate(item)
        if not relevant: return None, False
        job=self.repo.save(item,score,reasons,warnings)
        if not job: return None, True
        if notify and score>=self.config.min_notify_score: await self.notify(job)
        return job, True
    async def notify(self, job: Job):
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
