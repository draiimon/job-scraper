from __future__ import annotations
import asyncio, logging
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
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
cfg=settings(); repo=Repository(cfg.database_url); pipeline=Pipeline(repo,cfg)
async def worker():
    while True:
        await asyncio.gather(*(pipeline.run_source(s) for s in configured_sources(cfg.source_targets)))
        await pipeline.retry_notifications()
        await asyncio.sleep(cfg.poll_interval_seconds)
@asynccontextmanager
async def lifespan(app):
    repo.create_schema(); repo.expire_stale_jobs(); task=asyncio.create_task(worker()) if cfg.polling_enabled else None
    yield
    if task: task.cancel()
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
    return {'status':'ok','database':'ok','discord_configured':bool(cfg.discord_webhook_url),'gmail_configured':bool(cfg.google_client_id and cfg.google_client_secret),'ai':gemini().health(),'sources':{x.source:{'status':x.status,'jobs':x.last_job_count,'last_success':x.last_success_at,'consecutive_failures':x.consecutive_failures} for x in sources}}
@app.post('/run')
async def run_once(): return await asyncio.gather(*(pipeline.run_source(s) for s in configured_sources(cfg.source_targets)))
@app.get('/jobs')
def jobs(min_score:int=0,status:str|None=None,include_expired:bool=False):
    with repo.sessions() as s:
        q=select(Job).where(Job.score>=min_score)
        if status: q=q.where(Job.status==status)
        elif not include_expired: q=q.where(Job.status!=JobStatus.EXPIRED.value)
        return s.scalars(q.order_by(Job.date_discovered.desc()).limit(200)).all()
class StatusUpdate(BaseModel): status: JobStatus
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
