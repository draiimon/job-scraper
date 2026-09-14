from __future__ import annotations
import asyncio, logging
from datetime import datetime, timedelta, timezone
from html import escape
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
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
from .manual_search import ManualJobSearch
from .discord_bot import run_discord_bot
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
    saved.update({'service_status':'online','status':saved.get('status','starting'),'last_poll_at':saved.get('last_poll_at'),'next_poll_at':next_poll,'seconds_until_next_poll':seconds,'sources_working':sum(x.status=='healthy' for x in source_rows),'recent_jobs_found':len(recent),'discord_status':'READY' if cfg.discord_webhook_url else 'DISABLED','database_status':'CONNECTED','linkedin_status':'READY' if cfg.brightdata_api_token else 'DISABLED','jobstreet_status':'READY' if cfg.brightdata_enabled and cfg.brightdata_jobstreet_dataset_id and cfg.brightdata_inputs('jobstreet') else 'DISABLED'})
    return saved
async def poll_once():
    async with poll_lock:
        started=datetime.now(timezone.utc)
        repo.set_state('scheduler',{'status':'running','phase':'scanning','last_poll_at':iso(started),'next_poll_at':iso(started+timedelta(seconds=cfg.poll_interval_seconds)),'jobs_checked':0,'new_recent_jobs':0,'alerts_sent':0})
        if not cfg.discord_bot_token:
            try: await pipeline.discord.update_status(repo,scheduler_snapshot())
            except Exception as exc: logging.warning('discord_control_panel_update_failed',extra={'error':str(exc)})
        pipeline.begin_cycle()
        outcomes=await asyncio.gather(*(pipeline.run_source(s) for s in configured_sources(cfg.source_targets)+brightdata_sources(cfg,repo)))
        await pipeline.retry_notifications()
        finished=datetime.now(timezone.utc)
        checked=sum(x['discovered'] for x in outcomes); new=sum(x['new'] for x in outcomes); filtered=sum(x['filtered'] for x in outcomes)
        state={'status':'running','phase':'complete','last_poll_at':iso(finished),'next_poll_at':iso(finished+timedelta(seconds=cfg.poll_interval_seconds)),'jobs_checked':checked,'new_recent_jobs':new,'alerts_sent':pipeline._cycle_notifications,'duplicates_ignored':max(0,checked-new-filtered)}
        # status update is best-effort: a webhook outage never stops polling.
        state.update({'sources_working':0,'linkedin_status':'READY' if cfg.brightdata_api_token else 'DISABLED','jobstreet_status':'READY' if cfg.brightdata_enabled and cfg.brightdata_jobstreet_dataset_id and cfg.brightdata_inputs('jobstreet') else 'DISABLED'})
        repo.set_state('scheduler',state)
        if not cfg.discord_bot_token:
            try: await pipeline.discord.update_status(repo,scheduler_snapshot())
            except Exception as exc: logging.warning('discord_status_update_failed',extra={'error':str(exc)})
        return outcomes
async def worker():
    while True:
        await poll_once()
        await asyncio.sleep(cfg.poll_interval_seconds)
@asynccontextmanager
async def lifespan(app):
    repo.create_schema(); repo.expire_stale_jobs()
    try:
        if cfg.discord_bot_token: await pipeline.discord.remove_webhook_control_panel(repo)
        else: await pipeline.discord.update_status(repo,scheduler_snapshot())
    except Exception as exc: logging.warning('discord_control_panel_update_failed',extra={'error':str(exc)})
    task=asyncio.create_task(worker()) if cfg.polling_enabled else None
    bot_task=asyncio.create_task(run_discord_bot(cfg,repo,manual_search,scheduler_snapshot,manual_scan)) if cfg.discord_bot_token else None
    yield
    if task: task.cancel()
    if bot_task:
        bot_task.cancel()
        try: await bot_task
        except asyncio.CancelledError: pass
app=FastAPI(title='Philippine Job Agent',lifespan=lifespan)
@app.api_route('/',methods=['GET','HEAD'],include_in_schema=False,response_class=HTMLResponse)
def home():
    return HTMLResponse('''<!doctype html><html><head><title>Philippine Job Agent</title><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><h1>Philippine Job Agent</h1><p>Service is running.</p><ul><li><a href="/health">Health</a></li><li><a href="/docs">API documentation</a></li><li><a href="/jobs?min_score=60">Qualifying jobs</a></li></ul></body></html>''')
@app.get('/health')
def health():
    try:
        with repo.sessions() as s: s.execute(select(Job.id).limit(1))
    except Exception as e: raise HTTPException(503,detail='database unavailable') from e
    with repo.sessions() as s:
        sources=s.scalars(select(SourceHealth)).all()
    return {'status':'ok','database':'ok','discord_configured':bool(cfg.discord_webhook_url),'discord_bot':repo.state('discord_bot_health',{'healthy':False}),'secure_actions_configured':bool(cfg.app_secret_key and cfg.public_base_url),'gmail_configured':bool(cfg.google_client_id and cfg.google_client_secret),'scheduler':scheduler_snapshot(),'ai':gemini().health(),'sources':{x.source:{'status':x.status,'jobs':x.last_job_count,'last_success':x.last_success_at,'consecutive_failures':x.consecutive_failures} for x in sources}}
async def manual_scan():
    if poll_lock.locked(): raise HTTPException(409,'scan already running')
    previous=repo.state('manual_scan',{}) or {}; now=datetime.now(timezone.utc)
    if previous.get('at'):
        then=datetime.fromisoformat(previous['at'])
        remaining=cfg.manual_scan_cooldown_seconds-int((now-then).total_seconds())
        if remaining>0: raise HTTPException(429,f'scan cooldown: try again in {remaining} seconds')
    repo.set_state('manual_scan',{'at':iso(now)})
    return await poll_once()
@app.get('/control/scan/{token}',response_class=HTMLResponse)
def scan_confirmation(token:str):
    if not ActionTokens(cfg).verify_control(token,'scan'): raise HTTPException(403,'invalid or expired scan link')
    return HTMLResponse(f'''<!doctype html><title>Start scan</title><h1>Start one job scan</h1><p>This performs one immediate batch. Automatic scans continue normally.</p><button id="scan">START SCAN</button><p id="result"></p><script>document.getElementById('scan').onclick=async()=>{{let r=await fetch('/control/scan/{token}',{{method:'POST'}}),d=await r.json();document.getElementById('result').textContent=r.ok?'Scan complete.':(d.detail||'Scan unavailable.')}};</script>''')
@app.post('/control/scan/{token}')
async def scan_now(token:str):
    if not ActionTokens(cfg).verify_control(token,'scan'): raise HTTPException(403,'invalid or expired scan link')
    return await manual_scan()
@app.get('/status',response_class=HTMLResponse)
def status_page():
    state=scheduler_snapshot()
    return HTMLResponse(f'<h1>Job Hunter Status</h1><pre>{escape(str(state))}</pre><p><a href="/search">Search jobs</a></p>')
@app.get('/latest-page',response_class=HTMLResponse)
def latest_page():
    with repo.sessions() as s: rows=s.scalars(select(Job).where(Job.status!=JobStatus.EXPIRED.value,Job.score>=cfg.min_notify_score).order_by(Job.date_posted.desc()).limit(20)).all()
    cards=''.join(f'<article><h2>{escape(x.title)}</h2><p>{escape(x.company)} · {escape(x.location)} · {x.score}% match</p><p><a href="{escape(x.url,quote=True)}">View job</a></p></article>' for x in rows)
    return HTMLResponse(f'<h1>Latest qualifying jobs</h1>{cards or "<p>No recent qualifying jobs yet.</p>"}')
@app.get('/help',response_class=HTMLResponse)
def help_page():
    return HTMLResponse('''<h1>How to use After Hours Job Hunter</h1><h2>Auto scanning</h2><p>Runs every 15 minutes for recent PH entry-level tech roles.</p><h2>Scan now</h2><p>Runs one protected immediate scan and has a cooldown.</p><h2>Search jobs</h2><p>Find a specific role with freshness and location filters.</p><h2>Apply, Save, Skip</h2><p>Apply opens review, Save keeps a job for later, and Skip stops future alerts.</p>''')
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
    try:
        result=await manual_search.find(request.role,request.location,request.freshness,request.remote,request.limit,request.work_setup,request.min_score,request.entry_level_only,request.source_filter)
        return {'count':len(result),'jobs':result}
    except Exception as exc:
        raise HTTPException(503,detail='manual search unavailable; existing sources remain active') from exc
@app.get('/search',response_class=HTMLResponse)
def search_page():
    return HTMLResponse('''<!doctype html><html><head><title>Search latest jobs</title><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><h1>Search latest jobs</h1><p id="schedule">Loading scheduler status…</p><form id="search"><label>Job title / keyword <input name="role" required placeholder="Junior DevOps"></label><br><label>Location <select name="location"><option>Philippines</option><option>Remote Philippines</option><option>Manila</option></select></label><br><label>Freshness <select name="freshness"><option>Past 24 hours</option><option>Past 3 days</option><option>Past 7 days</option><option>Past 14 days</option></select></label><br><label>Work setup <select name="work_setup"><option value="">Any</option><option>Remote</option><option>Hybrid</option><option>On-site</option></select></label><br><label>Minimum match score <input name="min_score" value="0" min="0" max="100" type="number"></label><br><label>Entry-level only <input name="entry_level_only" checked type="checkbox"></label><br><label>Source <select name="source_filter"><option value="all">All available (LinkedIn)</option><option value="linkedin">LinkedIn</option></select></label><br><label>Result limit <input name="limit" value="10" min="1" max="10" type="number"></label><br><button>SEARCH</button></form><button id="send" hidden>SEND RESULTS TO DISCORD</button><main id="results"></main><script>let last=[],seconds=0;const out=document.getElementById('results'),schedule=document.getElementById('schedule');function show(j){let a=document.createElement('article'),h=document.createElement('h2'),p=document.createElement('p'),d=document.createElement('p'),l=document.createElement('a');h.textContent=j.title;p.textContent=j.company+' · '+j.location;d.textContent='Posted: '+(j.date_posted||'unavailable')+' · '+j.score+'% match';l.textContent='VIEW JOB';l.href=j.url;l.target='_blank';l.rel='noreferrer';a.append(h,p,d,l);out.append(a)}function clock(){if(seconds<=0){schedule.textContent='Scanning for new jobs…';setTimeout(loadStatus,3000);return}let m=Math.floor(seconds/60),s=String(seconds%60).padStart(2,'0');schedule.textContent='Next automatic scan in: '+m+':'+s;seconds--}async function loadStatus(){try{let d=await (await fetch('/health')).json();seconds=d.scheduler.seconds_until_next_poll||0;clock()}catch{schedule.textContent='Scheduler status unavailable'}}setInterval(clock,1000);loadStatus();document.getElementById('search').onsubmit=async e=>{e.preventDefault();let f=Object.fromEntries(new FormData(e.target));f.limit=+f.limit;f.min_score=+f.min_score;f.entry_level_only=!!f.entry_level_only;let r=await fetch('/find',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(f)});let d=await r.json();last=d.jobs||[];out.replaceChildren();if(last.length)last.forEach(show);else out.textContent='No recent qualifying jobs found.';document.getElementById('send').hidden=!last.length};document.getElementById('send').onclick=async()=>{let r=await fetch('/search/discord',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({jobs:last.map(j=>j.id)})});alert(r.ok?'Results sent to Discord.':'Could not send results.')};</script></body></html>''')
class DiscordSearchResults(BaseModel): jobs: list[int]
@app.post('/search/discord')
async def send_search_results(request: DiscordSearchResults):
    ids=list(dict.fromkeys(request.jobs))[:10]
    with repo.sessions() as s: jobs_found=[s.get(Job,job_id) for job_id in ids]
    sent=0
    for job in (x for x in jobs_found if x):
        try:
            if await pipeline.discord.send_payload(pipeline.discord.payload(job)): sent+=1
        except Exception as exc: logging.warning('manual_search_discord_failed',extra={'job_id':job.id,'error':str(exc)})
    return {'sent':sent}
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
        letter=await revised_cover_letter(job)
        path=write_package(job,letter=letter); eligible,reason=eligible_for_email(job,cfg.min_auto_application_score)
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
        letter=await revised_cover_letter(job); path=write_package(job,letter=letter)
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
