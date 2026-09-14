from __future__ import annotations
import asyncio, json, logging
from datetime import datetime, timedelta, timezone
from html import escape
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, File, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from .config import settings
from .jobs import NormalizedJob
from .models import Job, JobStatus, SourceRun, SourceHealth
from .services import Pipeline, Repository
from .sources import configured_sources
from .applications import eligible_for_email, write_package, revised_cover_letter
from .ai import gemini
from .security import ActionTokens
from .brightdata import brightdata_sources
from .jobstreet import jobstreet_sources, jobstreet_status
from .manual_search import ManualJobSearch
from .discord_bot import run_discord_bot
from .resumes import extract_resume_text
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
cfg=settings(); repo=Repository(cfg.database_url); pipeline=Pipeline(repo,cfg)
manual_search=ManualJobSearch(cfg,repo)
poll_lock=asyncio.Lock()
def iso(value): return value.astimezone(timezone.utc).isoformat()
def scheduler_snapshot():
    saved=repo.state('scheduler',{}) or {}
    with repo.sessions() as s:
        source_rows=s.scalars(select(SourceHealth)).all()
        recent=s.scalars(select(Job.id).where(Job.date_posted>=datetime.now(timezone.utc)-timedelta(days=7),Job.status!=JobStatus.EXPIRED.value)).all()
    next_poll=saved.get('next_poll_at'); seconds=0
    if next_poll:
        try: seconds=max(0,int((datetime.fromisoformat(next_poll)-datetime.now(timezone.utc)).total_seconds()))
        except ValueError: pass
        saved.update({'service_status':'online','status':saved.get('status','starting'),'last_poll_at':saved.get('last_poll_at'),'next_poll_at':next_poll,'seconds_until_next_poll':seconds,'sources_working':sum(x.status=='healthy' for x in source_rows),'recent_jobs_found':len(recent),'discord_status':'READY' if cfg.discord_bot_token or cfg.discord_webhook_url else 'DISABLED','database_status':'CONNECTED','linkedin_status':'READY' if cfg.brightdata_enabled and cfg.brightdata_api_token and cfg.brightdata_linkedin_jobs_dataset_id and cfg.brightdata_inputs('linkedin_jobs') else 'DISABLED','jobstreet_status':jobstreet_status(cfg,repo)})
    return saved
async def poll_once(manual=False):
    async with poll_lock:
        started=datetime.now(timezone.utc)
        previous=repo.state('scheduler',{}) or {}
        scheduled_next=previous.get('next_poll_at') if manual else None
        if not scheduled_next: scheduled_next=iso(started+timedelta(seconds=cfg.poll_interval_seconds))
        repo.set_state('scheduler',{'status':'running','phase':'scanning','last_poll_at':iso(started),'next_poll_at':scheduled_next,'jobs_checked':0,'new_recent_jobs':0,'alerts_sent':0})
        if not cfg.discord_bot_token:
            try: await pipeline.discord.update_status(repo,scheduler_snapshot())
            except Exception as exc: logging.warning('discord_control_panel_update_failed',extra={'error':str(exc)})
        try:
            pipeline.begin_cycle(); public_sources=configured_sources(cfg.source_targets); sources=public_sources+brightdata_sources(cfg,repo)+jobstreet_sources(cfg,repo)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            finished=datetime.now(timezone.utc)
            repo.set_state('scheduler',{'status':'error','phase':'complete','last_poll_at':iso(finished),'next_poll_at':scheduled_next if manual else iso(finished+timedelta(seconds=cfg.poll_interval_seconds)),'jobs_checked':0,'new_recent_jobs':0,'alerts_sent':0,'sources_loaded':0,'source_config_error':str(exc)})
            logging.error('source_configuration_failed: %s',exc)
            return []
        semaphore=asyncio.Semaphore(cfg.scan_source_concurrency)
        async def limited(source):
            async with semaphore:
                try: return await asyncio.wait_for(pipeline.run_source(source),timeout=cfg.scan_source_timeout_seconds)
                except asyncio.TimeoutError:
                    repo.health_failure(source.name,'scan source timeout')
                    return {'source':source.name,'discovered':0,'new':0,'filtered':0,'timeout':True}
        outcomes=await asyncio.gather(*(limited(source) for source in sources))
        await pipeline.retry_notifications()
        finished=datetime.now(timezone.utc)
        checked=sum(x['discovered'] for x in outcomes); new=sum(x['new'] for x in outcomes); filtered=sum(x['filtered'] for x in outcomes)
        state={'status':'running','phase':'complete','last_poll_at':iso(finished),'next_poll_at':scheduled_next if manual else iso(finished+timedelta(seconds=cfg.poll_interval_seconds)),'jobs_checked':checked,'new_recent_jobs':new,'alerts_sent':pipeline._cycle_notifications,'duplicates_ignored':max(0,checked-new-filtered),'sources_loaded':len(sources)}
        # status update is best-effort: a webhook outage never stops polling.
        state.update({'sources_working':sum(1 for source in sources if repo.health(source.name).status=='healthy'),'linkedin_status':'READY' if cfg.brightdata_enabled and cfg.brightdata_api_token and cfg.brightdata_linkedin_jobs_dataset_id and cfg.brightdata_inputs('linkedin_jobs') else 'DISABLED','jobstreet_status':jobstreet_status(cfg,repo)})
        repo.set_state('scheduler',state)
        if not cfg.discord_bot_token:
            try: await pipeline.discord.update_status(repo,scheduler_snapshot())
            except Exception as exc: logging.warning('discord_status_update_failed',extra={'error':str(exc)})
        return outcomes
async def worker():
    while True:
        try:
            await poll_once()
        except Exception:
            # A failed cycle must not kill the long-lived 15-minute scheduler.
            logging.exception('automatic_poll_failed')
        state=repo.state('scheduler',{}) or {}
        next_poll=state.get('next_poll_at')
        try:
            delay=max(0,(datetime.fromisoformat(next_poll)-datetime.now(timezone.utc)).total_seconds()) if next_poll else cfg.poll_interval_seconds
        except (TypeError, ValueError):
            delay=cfg.poll_interval_seconds
        await asyncio.sleep(delay)
def discord_task_done(task):
    if task.cancelled(): return
    error=task.exception()
    if error:
        logging.error('discord_bot_task_failed',extra={'error':str(error)},exc_info=(type(error),error,error.__traceback__))
    else:
        logging.warning('discord_bot_stopped')
@asynccontextmanager
async def lifespan(app):
    repo.create_schema(); repo.expire_stale_jobs()
    try:
        if cfg.discord_bot_token: await pipeline.discord.remove_webhook_control_panel(repo)
        else: await pipeline.discord.update_status(repo,scheduler_snapshot())
    except Exception as exc: logging.warning('discord_control_panel_update_failed',extra={'error':str(exc)})
    task=asyncio.create_task(worker()) if cfg.polling_enabled else None
    bot_task=asyncio.create_task(run_discord_bot(cfg,repo,manual_search,scheduler_snapshot,manual_scan)) if cfg.discord_bot_token else None
    if bot_task: bot_task.add_done_callback(discord_task_done)
    yield
    if task: task.cancel()
    if bot_task:
        bot_task.cancel()
        try: await bot_task
        except asyncio.CancelledError: pass
app=FastAPI(title='After Hours Job Hunter',lifespan=lifespan)
@app.api_route('/',methods=['GET','HEAD'],include_in_schema=False,response_class=HTMLResponse)
def home():
    return HTMLResponse('''<!doctype html><html><head><title>After Hours Job Hunter</title><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><main><h1>After Hours Job Hunter</h1><p>Job hunting is managed in Discord. Use <code>v!search</code>, <code>v!latest</code>, <code>v!status</code>, <code>v!scan</code>, or <code>v!help</code>.</p></main></body></html>''')

@app.get('/favicon.ico',include_in_schema=False)
def favicon():
    return Response(status_code=204)

def resume_upload_link():
    token=ActionTokens(cfg).issue_control('resume')
    return f'/resume/{token}' if token else None

@app.get('/resume',response_class=HTMLResponse)
def resume_status_page():
    info=repo.resume_info()
    link=resume_upload_link()
    current=f'<p><strong>Saved resume:</strong> {escape(info["filename"])}<br><strong>Uploaded:</strong> {escape(str(info["uploaded_at"]))}</p>' if info else '<p>No resume is saved in the database yet.</p>'
    upload=f'<p><a href="{link}">Open secure resume upload</a></p>' if link else '<p>Set APP_SECRET_KEY to enable secure resume uploads.</p>'
    return HTMLResponse(f'<!doctype html><title>Resume</title><h1>Resume context</h1>{current}<p>The saved PDF and extracted text are used when generating cover letters. Uploading a new PDF replaces the saved resume and clears cached letters.</p>{upload}<p><a href="/">Back to job hunter</a></p>')

@app.get('/resume/{token}',response_class=HTMLResponse)
def resume_upload_page(token:str):
    if not ActionTokens(cfg).verify_control(token,'resume'): raise HTTPException(403,'invalid or expired resume upload link')
    return HTMLResponse(f'''<!doctype html><title>Upload resume</title><h1>Upload or replace resume</h1><p>PDF only, maximum 10 MB. This will replace the resume currently used for cover letters.</p><form method="post" enctype="multipart/form-data"><input type="file" name="resume" accept=".pdf,application/pdf" required><button type="submit">SAVE RESUME</button></form><p><a href="/resume">Cancel</a></p>''')

@app.post('/resume/{token}',response_class=HTMLResponse)
async def upload_resume(token:str, resume:UploadFile=File(...)):
    if not ActionTokens(cfg).verify_control(token,'resume'): raise HTTPException(403,'invalid or expired resume upload link')
    data=await resume.read()
    try:
        text=extract_resume_text(data,resume.filename or 'resume.pdf')
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc
    info=repo.save_resume(resume.filename or 'resume.pdf',resume.content_type or 'application/pdf',data,text)
    return HTMLResponse(f'<h1>Resume saved</h1><p>{escape(info["filename"])} is now the source of truth for new cover letters.</p><p>Existing cached cover letters were cleared so the next APPLY NOW uses this resume.</p><p><a href="/resume">View resume status</a></p>')
@app.get('/health')
def health():
    try:
        with repo.sessions() as s: s.execute(select(Job.id).limit(1))
    except Exception as e: raise HTTPException(503,detail='database unavailable') from e
    with repo.sessions() as s:
        sources=s.scalars(select(SourceHealth)).all()
    return {'status':'ok','database':'ok','discord_configured':bool(cfg.discord_bot_token or cfg.discord_webhook_url),'discord_bot':repo.state('discord_bot_health',{'healthy':False}),'resume':repo.resume_info(),'secure_actions_configured':bool(cfg.app_secret_key and cfg.public_base_url),'gmail_configured':bool(cfg.google_client_id and cfg.google_client_secret),'scheduler':scheduler_snapshot(),'ai':gemini().health(),'sources':{x.source:{'status':x.status,'jobs':x.last_job_count,'last_success':x.last_success_at,'consecutive_failures':x.consecutive_failures} for x in sources}}
async def manual_scan():
    if poll_lock.locked(): raise HTTPException(409,'scan already running')
    previous=repo.state('manual_scan',{}) or {}; now=datetime.now(timezone.utc)
    if previous.get('at'):
        then=datetime.fromisoformat(previous['at'])
        remaining=cfg.manual_scan_cooldown_seconds-int((now-then).total_seconds())
        if remaining>0: raise HTTPException(429,f'scan cooldown: try again in {remaining} seconds')
    repo.set_state('manual_scan',{'at':iso(now)})
    return await poll_once(manual=True)
@app.get('/control/scan/{token}',response_class=HTMLResponse)
def scan_confirmation(token:str):
    raise HTTPException(410,'Manual scans are available in Discord with v!scan')
@app.post('/control/scan/{token}')
async def scan_now(token:str):
    raise HTTPException(410,'Manual scans are available in Discord with v!scan')
@app.get('/status',response_class=HTMLResponse)
def status_page():
    raise HTTPException(410,'Status is available in Discord with v!status')
@app.get('/latest-page',response_class=HTMLResponse)
def latest_page():
    raise HTTPException(410,'Latest jobs are available in Discord with v!latest')
@app.get('/help',response_class=HTMLResponse)
def help_page():
    raise HTTPException(410,'Help is available in Discord with v!help')
@app.get('/jobs')
def jobs(min_score:int=0,status:str|None=None,include_expired:bool=False):
    with repo.sessions() as s:
        q=select(Job).where(Job.score>=min_score)
        if status: q=q.where(Job.status==status)
        elif not include_expired: q=q.where(Job.status!=JobStatus.EXPIRED.value)
        return s.scalars(q.order_by(Job.date_discovered.desc()).limit(200)).all()
class StatusUpdate(BaseModel): status: JobStatus
class FindRequest(BaseModel):
    role: str
    location: str = 'Philippines'
    freshness: str = 'Past 24 hours'
    remote: str = ''
    limit: int = 10
    work_setup: str = ''
    min_score: int = 0
    entry_level_only: bool = True
    source_filter: str = 'all'
@app.post('/find')
async def find_jobs(request: FindRequest):
    raise HTTPException(410,'Search is available in Discord with v!search')
@app.get('/search',response_class=HTMLResponse)
def search_page():
    raise HTTPException(410,'Search is available in Discord with v!search')
class DiscordSearchResults(BaseModel): jobs: list[int]
@app.post('/search/discord')
async def send_search_results(request: DiscordSearchResults):
    raise HTTPException(410,'Discord search results are delivered by v!search')
@app.get('/latest')
def latest_jobs(limit:int=10):
    with repo.sessions() as s:
        return s.scalars(select(Job).where(Job.status!=JobStatus.EXPIRED.value,Job.score>=cfg.min_notify_score,Job.date_posted.is_not(None)).order_by(Job.date_posted.desc()).limit(max(1,min(limit,25)))).all()
@app.patch('/jobs/{job_id}/status')
def update_status(job_id:int, update:StatusUpdate):
    with repo.sessions() as s:
        job=s.get(Job,job_id)
        if not job: raise HTTPException(404,'job not found')
        job.status=update.status.value; s.commit(); return job
@app.post('/jobs/{job_id}/prepare-application')
async def prepare_application(job_id:int):
    with repo.sessions() as s:
        job=s.get(Job,job_id)
        if not job: raise HTTPException(404,'job not found')
        # File generation is intentionally separate from sending; OAuth email sending remains opt-in.
        resume=repo.resume_record()
        letter=await revised_cover_letter(job,repo.resume_text())
        path=write_package(job,letter=letter,resume_bytes=resume['file_data'] if resume else None,resume_filename=resume['filename'] if resume else None); eligible,reason=eligible_for_email(job,cfg.min_auto_application_score)
        return {'path':str(path),'email_eligible':eligible,'reason':reason,'auto_send_enabled':cfg.auto_send_email_applications}
def action_job(token:str):
    payload=ActionTokens(cfg).verify(token)
    if not payload: raise HTTPException(403,'invalid or expired action link')
    with repo.sessions() as s:
        job=s.get(Job,payload['j'])
        if not job: raise HTTPException(404,'job not found')
        return payload,job
@app.get('/actions/{token}',response_class=HTMLResponse)
def action_page(token:str):
    payload,job=action_job(token); action=payload['a']
    if action=='review':
        body=f'<h1>{escape(job.title)}</h1><p>{escape(job.company)} · {escape(job.location)}</p><p>Application method: {"published email" if job.application_email else "job listing"}</p><p><a href="{escape(job.application_url or job.url,quote=True)}">View original listing</a></p><p>Use a signed action below; no email is sent from this page.</p>'
    else: body=f'<h1>{escape(action.replace("_"," ").title())}</h1><p>{escape(job.title)} · {escape(job.company)}</p>'
    return HTMLResponse(body+f'''<button onclick="fetch('/actions/{token}',{{method:'POST'}}).then(r=>r.text()).then(t=>document.body.insertAdjacentHTML('beforeend','<p>'+t+'</p>'))">Confirm</button>''')
@app.post('/actions/{token}')
async def action_post(token:str):
    payload,job=action_job(token); action=payload['a']
    if action=='generate':
        resume=repo.resume_record()
        letter=await revised_cover_letter(job,repo.resume_text()); path=write_package(job,letter=letter,resume_bytes=resume['file_data'] if resume else None,resume_filename=resume['filename'] if resume else None)
        return {'result':'cover letter generated','path':str(path)}
    if action in {'applied','saved','ignored'}:
        status={'applied':'APPLIED','saved':'SAVED','ignored':'IGNORED'}[action]
        with repo.sessions() as s:
            stored=s.get(Job,job.id)
            if stored.status!=status: stored.status=status; s.commit()
        return {'result':status}
    if action=='remind':
        with repo.sessions() as s:
            stored=s.get(Job,job.id); stored.raw_metadata={**stored.raw_metadata,'reminder_requested':True}; s.commit()
        return {'result':'reminder saved'}
    if action=='review': return {'result':'review ready'}
    raise HTTPException(400,'unsupported action')
@app.get('/source-runs')
def source_runs():
    with repo.sessions() as s: return s.scalars(select(SourceRun).order_by(SourceRun.id.desc()).limit(100)).all()
@app.post('/fixtures/smoke')
async def smoke():
    item=NormalizedJob(source='fixture',source_job_id='junior-devops-001',title='Junior DevOps Engineer',company='Fixture Cloud Inc',location='Makati, Philippines — Hybrid',description='Fresh graduate role. AWS Docker Terraform Linux CI/CD GitHub Actions Kubernetes.',url='https://example.invalid/jobs/junior-devops-001')
    job, accepted=await pipeline.process(item)
    return {'accepted':accepted,'created':bool(job),'job_id':job.id if job else None}
