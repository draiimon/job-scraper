from __future__ import annotations
import asyncio, logging, random, json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import httpx
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from .config import Settings
from .jobs import NormalizedJob, evaluate, extract_skills, is_ph_location, freshness, is_active_listing
from .models import Base, Job, JobStatus, SourceRun, SourceHealth, AppState, AppSetting, ResumeProfile, JobEvent
from .security import ActionTokens
log=logging.getLogger(__name__)
DISCORD_ALERT_ROLE_ALLOWLIST={'1346328166100107366'}
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
    def initialize_runtime_config(self, cfg: Settings) -> dict[str, str]:
        """Create/load safe runtime settings and hydrate the process config.

        Non-secret values live in app_settings. Browserless's bearer token is
        migrated to Supabase Vault when that extension is available and is
        never copied into a normal application table.
        """
        self.create_schema()
        defaults = {
            'browserless_endpoint': 'https://production-sfo.browserless.io',
            'jobstreet_enabled': 'true',
            'jobstreet_base_url': 'https://ph.jobstreet.com',
            'jobstreet_login_url': '',
            'jobstreet_location': 'Philippines',
            'jobstreet_search_terms_json': '["DevOps", "Cloud", "IT Support"]',
            'jobstreet_max_results': '50',
            'jobstreet_auth_timeout_seconds': '600',
            'jobstreet_scan_timeout_seconds': '60',
            'jobstreet_last_verified': '',
            'jobstreet_status': 'AUTH REQUIRED',
        }
        with self.sessions.begin() as session:
            for key, value in defaults.items():
                if session.get(AppSetting, key) is None:
                    session.add(AppSetting(key=key, value=value))
        with self.sessions() as session:
            values = {
                row.key: row.value
                for row in session.scalars(select(AppSetting)).all()
            }
        endpoint = str(values.get('browserless_endpoint') or '').strip()
        if endpoint:
            cfg.browserless_endpoint = endpoint
        enabled = str(values.get('jobstreet_enabled', 'true')).strip().lower()
        cfg.jobstreet_enabled = enabled not in {'0', 'false', 'no', 'off'}
        for key in (
            'jobstreet_base_url',
            'jobstreet_login_url',
            'jobstreet_location',
            'jobstreet_search_terms_json',
        ):
            value = str(values.get(key) or '')
            if value:
                setattr(cfg, key, value)
        for key in (
            'jobstreet_max_results',
            'jobstreet_auth_timeout_seconds',
            'jobstreet_scan_timeout_seconds',
        ):
            try:
                setattr(cfg, key, int(values[key]))
            except (KeyError, TypeError, ValueError):
                pass
        vault_token = self._vault_secret('browserless_api_token')
        if vault_token:
            cfg.browserless_api_token = vault_token
        elif cfg.browserless_api_token:
            # The env secret remains a bootstrap input only. On Supabase it is
            # copied into Vault once; the next restart reads Vault instead.
            self._vault_store_secret('browserless_api_token', cfg.browserless_api_token)
        return values

    def set_setting(self, key: str, value: str) -> None:
        with self.sessions.begin() as session:
            setting = session.get(AppSetting, key)
            if setting is None:
                session.add(AppSetting(key=key, value=str(value)))
            else:
                setting.value = str(value)

    def _vault_secret(self, name: str) -> str | None:
        if self.engine.dialect.name != 'postgresql':
            return None
        try:
            with self.sessions() as session:
                value = session.execute(
                    text('SELECT secret FROM vault.decrypted_secrets WHERE name = :name LIMIT 1'),
                    {'name': name},
                ).scalar()
            return str(value) if value else None
        except Exception as exc:
            log.info('vault_unavailable', extra={'error_type': type(exc).__name__})
            return None

    def _vault_store_secret(self, name: str, value: str) -> bool:
        if self.engine.dialect.name != 'postgresql':
            return False
        try:
            with self.sessions.begin() as session:
                session.execute(
                    text('SELECT vault.create_secret(:secret, :name, :description)'),
                    {
                        'secret': value,
                        'name': name,
                        'description': 'After Hours Job Hunter runtime secret',
                    },
                )
            return True
        except Exception as exc:
            # Do not include the secret or provider response in logs.
            log.info('vault_secret_migration_unavailable', extra={'secret_name': name, 'error_type': type(exc).__name__})
            return False
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
    def acquire_discord_bot_lease(self, instance_id: str, ttl_seconds: int = 60) -> bool:
        """Acquire the renewable gateway lease without ever storing a token.

        Row locking makes this atomic on Postgres; SQLite serializes the small
        write transaction used by local tests/development.
        """
        key='discord_bot_gateway_lease'; now=datetime.now(timezone.utc)
        expires=now+timedelta(seconds=max(15, ttl_seconds))
        with self.sessions.begin() as s:
            row=s.execute(select(AppState).where(AppState.key==key).with_for_update()).scalar_one_or_none()
            current={}
            if row:
                try: current=json.loads(row.value)
                except json.JSONDecodeError: current={}
            owner=str(current.get('owner') or '')
            try:
                valid_until=datetime.fromisoformat(str(current.get('expires_at')))
                if valid_until.tzinfo is None: valid_until=valid_until.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError): valid_until=now-timedelta(seconds=1)
            if owner and owner != instance_id and valid_until > now:
                return False
            payload={'owner':instance_id,'expires_at':expires.isoformat()}
            if row: row.value=json.dumps(payload)
            else: s.add(AppState(key=key,value=json.dumps(payload)))
        return True
    def renew_discord_bot_lease(self, instance_id: str, ttl_seconds: int = 60) -> bool:
        key='discord_bot_gateway_lease'; now=datetime.now(timezone.utc)
        with self.sessions.begin() as s:
            row=s.execute(select(AppState).where(AppState.key==key).with_for_update()).scalar_one_or_none()
            if not row: return False
            try: current=json.loads(row.value)
            except json.JSONDecodeError: return False
            if current.get('owner') != instance_id: return False
            row.value=json.dumps({'owner':instance_id,'expires_at':(now+timedelta(seconds=max(15,ttl_seconds))).isoformat()})
        return True
    def release_discord_bot_lease(self, instance_id: str) -> None:
        key='discord_bot_gateway_lease'
        with self.sessions.begin() as s:
            row=s.execute(select(AppState).where(AppState.key==key).with_for_update()).scalar_one_or_none()
            if not row: return
            try: current=json.loads(row.value)
            except json.JSONDecodeError: current={}
            if current.get('owner') == instance_id:
                row.value=json.dumps({'owner':'','expires_at':datetime.now(timezone.utc).isoformat()})
    def claim_bot_alert(self, job_id: int) -> bool:
        """Reserve a pending alert before sending so it cannot ping twice."""
        with self.sessions.begin() as s:
            job=s.get(Job,job_id)
            if not job or job.notification_state != 'BOT_PENDING':
                return False
            job.notification_state='BOT_SENDING'
        return True
    def set_job_status(self, job_id: int, status: str, detail: str = '') -> bool:
        """Persist a state change and its audit event in one transaction."""
        with self.sessions.begin() as s:
            job=s.get(Job,job_id)
            if not job: return False
            if job.status == status: return True
            previous=job.status; job.status=status
            s.add(JobEvent(job_id=job_id,event_type=status,detail=detail or f'{previous} → {status}'))
        return True
    def record_job_event(self, job_id: int, event_type: str, detail: str = '') -> None:
        with self.sessions.begin() as s:
            if s.get(Job,job_id): s.add(JobEvent(job_id=job_id,event_type=event_type,detail=detail))
    def resume_record(self):
        with self.sessions() as s:
            item=s.get(ResumeProfile,1)
            if not item: return None
            return {'filename':item.filename,'content_type':item.content_type,'file_data':item.file_data,'extracted_text':item.extracted_text,'uploaded_at':item.uploaded_at}
    def resume_info(self):
        item=self.resume_record()
        if not item: return None
        return {key:item[key] for key in ('filename','content_type','uploaded_at')}
    def resume_text(self):
        item=self.resume_record()
        return item['extracted_text'] if item else None
    def save_resume(self, filename: str, content_type: str, file_data: bytes, extracted_text: str):
        with self.sessions() as s:
            item=s.get(ResumeProfile,1)
            if not item:
                item=ResumeProfile(id=1,filename=filename,content_type=content_type,file_data=file_data,extracted_text=extracted_text)
                s.add(item)
            else:
                item.filename=filename; item.content_type=content_type; item.file_data=file_data; item.extracted_text=extracted_text; item.uploaded_at=datetime.now(timezone.utc)
            for job in s.scalars(select(Job)).all():
                metadata=dict(job.raw_metadata or {})
                metadata.pop('cover_letter',None); metadata.pop('cover_letter_mode',None)
                job.raw_metadata=metadata
            s.commit()
            return self.resume_info()
class Discord:
    def __init__(self,url:str|None, motivations:list[str]|str='', cfg:Settings|None=None):
        configured=motivations if isinstance(motivations,list) else [motivations]
        self.url=url; self.motivations=[x for x in configured if x] or ['']; self.cfg=cfg
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
        if row2: rows.append({'type':1,'components':row2})
        kind,name=(job.source.split(':',1)+[''])[:2] if ':' in job.source else (job.source,'')
        footer=f'{kind.replace("_"," ").title()}' + (f' · {name}' if name else '')
        if test: footer='𝐓𝐄𝐒𝐓 𝐀𝐋𝐄𝐑𝐓 — No real application will be sent.'
        fields.append({'name':'𝐒𝐓𝐀𝐓𝐔𝐒','value':'Ready to review' if job.status not in ('APPLIED','IGNORED') else job.status.title(),'inline':False})
        if self.cfg and self.cfg.public_base_url:
            fields.append({'name':'𝐖𝐀𝐍𝐓 𝐓𝐎 𝐅𝐈𝐍𝐃 𝐀 𝐒𝐏𝐄𝐂𝐈𝐅𝐈𝐂 𝐉𝐎𝐁?','value':'Find or filter the exact role you want to apply for here.','inline':False})
        configured_role=(self.cfg.discord_alert_role_id if self.cfg else None)
        role_id=configured_role if configured_role in DISCORD_ALERT_ROLE_ALLOWLIST else None
        role_mention=f'<@&{role_id}>' if role_id and not test else ''
        content='\n\n'.join(part for part in (role_mention,random.choice(self.motivations)) if part)
        allowed={'parse':[], 'roles':[str(role_id)]} if role_id and not test else {'parse':[]}
        return {'content':content,'allowed_mentions':allowed,'embeds':[{'title':label,'description':f'**{job.score}% MATCH**\n\n**{job.title}**\n{job.company}\n\n'+' · '.join(details),'url':job.url,'fields':fields,'footer':{'text':footer}}],'components':[] if test else rows}
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
        payload={'embeds':[{'title':'𝐀𝐅𝐓𝐄𝐑 𝐇𝐎𝐔𝐑𝐒 𝐉𝐎𝐁 𝐇𝐔𝐍𝐓𝐄𝐑','description':'Your automated Philippine tech-job monitor is online.','color':0xF59E0B,'fields':[{'name':'𝐒𝐘𝐒𝐓𝐄𝐌 𝐒𝐓𝐀𝐓𝐔𝐒','value':system,'inline':True},{'name':'𝐂𝐎𝐍𝐍𝐄𝐂𝐓𝐈𝐎𝐍𝐒','value':sources_text,'inline':True},{'name':'𝐒𝐂𝐀𝐍𝐍𝐈𝐍𝐆 𝐍𝐎𝐖' if phase=='scanning' else '𝐒𝐂𝐀𝐍 𝐂𝐎𝐌𝐏𝐋𝐄𝐓𝐄','value':scan_text,'inline':False},{'name':'𝐇𝐎𝐖 𝐓𝐎 𝐔𝐒𝐄','value':'Auto scanning runs every 15 minutes. Use v!search for a specific role, v!latest for stored jobs, v!status for health, or v!scan for one protected immediate batch.','inline':False}],'footer':{'text':'After Hours Job Hunter • Made by masoncalix'}}]}
        existing=repo.state('discord_control_panel_message_id') or repo.state('discord_status_message_id')
        async with httpx.AsyncClient(timeout=15) as client:
            # Remove the legacy separate welcome message once during migration;
            # the control panel now owns both status and tutorial content.
            legacy=repo.state('discord_welcome_message_id')
            if legacy:
                removed=await client.delete(f'{self.url}/messages/{legacy}')
                if removed.status_code in (204,404): repo.set_state('discord_welcome_message_id',None)
            if existing:
                response=await client.patch(f'{self.url}/messages/{existing}',params={'with_components':'true'},json=payload)
                if response.status_code==404: existing=None
                else: response.raise_for_status(); return 'UPDATED'
            response=await client.post(self.url,params={'wait':'true','with_components':'true'},json=payload); response.raise_for_status()
            message_id=response.json().get('id')
            if message_id: repo.set_state('discord_control_panel_message_id',message_id)
        return 'CREATED'
    async def remove_webhook_control_panel(self, repo: Repository):
        """Migrate from webhook link components to the bot-owned panel."""
        if not self.url: return
        ids=[repo.state('discord_control_panel_message_id'),repo.state('discord_status_message_id'),repo.state('discord_welcome_message_id')]
        async with httpx.AsyncClient(timeout=15) as client:
            for message_id in {x for x in ids if x}:
                response=await client.delete(f'{self.url}/messages/{message_id}')
                if response.status_code not in (204,404): response.raise_for_status()
        repo.set_state('discord_control_panel_message_id',None); repo.set_state('discord_status_message_id',None); repo.set_state('discord_welcome_message_id',None)
class Pipeline:
    def __init__(self, repo:Repository, config:Settings): self.repo=repo; self.config=config; self.discord=Discord(config.discord_webhook_url,config.discord_motivations,config); self._baseline_lock=asyncio.Lock(); self._cycle_lock=asyncio.Lock(); self._cycle_notifications=0
    def begin_cycle(self): self._cycle_notifications=0
    async def process(self, item:NormalizedJob, notify=True) -> tuple[Job|None,bool]:
        if not is_ph_location(item) or not is_active_listing(item): return None, False
        score,reasons,warnings,relevant=evaluate(item)
        _,_,fresh=freshness(item)
        if not relevant or not fresh or (freshness(item)[0] < 0 and score < 85): return None, False
        job=await asyncio.to_thread(self.repo.save,item,score,reasons,warnings)
        if not job: return None, True
        if notify and score>=self.config.min_notify_score: await self.notify(job)
        return job, True
    async def notify(self, job: Job):
        async with self._cycle_lock:
            if self._cycle_notifications>=self.config.max_notifications_per_cycle: return
            self._cycle_notifications+=1
        bot_state=await asyncio.to_thread(self.repo.state,'discord_bot_health',{}) or {}
        # Before the first successful bot connection, hold alerts for the bot
        # instead of racing a startup webhook. Once a known bot disconnects,
        # the webhook is the emergency fallback.
        if self.config.discord_bot_token and (bot_state.get('healthy') or not bot_state.get('started_once')):
            job.notification_state='BOT_PENDING'
            def hold_for_bot():
                with self.repo.sessions() as s:
                    stored=s.get(Job,job.id)
                    if stored: stored.notification_state='BOT_PENDING'; s.commit()
            await asyncio.to_thread(hold_for_bot)
            return
        try:
            job.notification_state=await self.discord.send(job)
            if job.notification_state=='SENT': job.status=JobStatus.NOTIFIED.value
        except httpx.HTTPError as e:
            job.notification_state='FAILED'; log.warning('discord_notification_failed',extra={'job_id':job.id,'error':str(e)})
        def persist_notification():
            with self.repo.sessions() as s:
                stored=s.get(Job,job.id)
                if stored:
                    stored.notification_state=job.notification_state; stored.status=job.status; s.commit()
        await asyncio.to_thread(persist_notification)
    async def retry_notifications(self):
        if not self.config.discord_webhook_url: return 0
        def failed_ids():
            with self.repo.sessions() as s:
                return s.scalars(select(Job.id).where(Job.notification_state=='FAILED',Job.score>=self.config.min_notify_score)).all()
        ids=await asyncio.to_thread(failed_ids)
        for job_id in ids:
            def load_job():
                with self.repo.sessions() as s: return s.get(Job,job_id)
            job=await asyncio.to_thread(load_job)
            if job: await self.notify(job)
        return len(ids)
    async def run_source(self,source):
        run=await asyncio.to_thread(self.repo.run_start,source.name); discovered=new=filtered=0
        try:
            for attempt in range(3):
                try: items=await source.fetch(); break
                except Exception:
                    if attempt==2: raise
                    await asyncio.sleep(2**attempt)
            health=await asyncio.to_thread(self.repo.health,source.name)
            baseline=not health.baseline_initialized
            ordered=sorted(items,key=lambda x:evaluate(x)[0],reverse=True)
            baseline_candidates=[]
            for item in ordered:
                discovered+=1; job,accepted=await self.process(item,notify=not baseline); filtered+=not accepted; new+=job is not None
                if baseline and job and job.score>=self.config.min_notify_score: baseline_candidates.append(job)
            if baseline:
                for job in baseline_candidates:
                    async with self._baseline_lock:
                        allowed=await asyncio.to_thread(self.repo.reserve_baseline_alert)
                    if not allowed: break
                    await self.notify(job)
            await asyncio.to_thread(self.repo.run_finish,run,success=True,discovered=discovered,new_jobs=new,filtered=filtered)
            await asyncio.to_thread(self.repo.health_success,source.name,discovered,baseline=True)
        except Exception as e:
            await asyncio.to_thread(self.repo.run_finish,run,success=False,error=str(e)); log.warning('source_failed',extra={'source':source.name,'error':str(e)})
            await asyncio.to_thread(self.repo.health_failure,source.name,str(e))
        return {'source':source.name,'discovered':discovered,'new':new,'filtered':filtered}
