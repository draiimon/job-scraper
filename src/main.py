from __future__ import annotations
import asyncio, csv, io, json, logging
from datetime import datetime, timedelta, timezone
from html import escape
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, File, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from .config import settings
from .jobs import NormalizedJob
from .models import Job, JobStatus, SourceRun, SourceHealth, JobEvent
from .jobstreet_link import (
    browserless_ready,
    interactive_session_for_request,
    request_for_token,
    start_interactive_session,
)
from .services import Pipeline, Repository
from .sources import configured_sources
from .applications import eligible_for_email, write_package, revised_cover_letter
from .ai import gemini
from .security import ActionTokens
from .brightdata import brightdata_sources
from .jobstreet import brightdata_jobstreet_status, jobstreet_sources, jobstreet_status
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
        saved.update({'service_status':'online','status':saved.get('status','starting'),'last_poll_at':saved.get('last_poll_at'),'next_poll_at':next_poll,'seconds_until_next_poll':seconds,'sources_working':sum(x.status=='healthy' for x in source_rows),'recent_jobs_found':len(recent),'discord_status':'READY' if cfg.discord_bot_token or cfg.discord_webhook_url else 'DISABLED','database_status':'CONNECTED','linkedin_status':'READY' if cfg.brightdata_enabled and cfg.brightdata_api_token and cfg.brightdata_linkedin_jobs_dataset_id and cfg.brightdata_inputs('linkedin_jobs') else 'DISABLED','jobstreet_status':jobstreet_status(cfg,repo),'jobstreet_brightdata_status':brightdata_jobstreet_status(cfg)})
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
                timeout = (
                    cfg.jobstreet_scan_timeout_seconds
                    if source.name == 'jobstreet:google-session'
                    else cfg.brightdata_linkedin_scan_timeout_seconds
                    if source.name == 'brightdata:linkedin_jobs'
                    else cfg.scan_source_timeout_seconds
                )
                try: return await asyncio.wait_for(pipeline.run_source(source),timeout=timeout)
                except asyncio.TimeoutError:
                    repo.health_failure(source.name,'scan source timeout')
                    return {'source':source.name,'discovered':0,'new':0,'filtered':0,'timeout':True,'success':False,'pages_fetched':0}
        outcomes=await asyncio.gather(*(limited(source) for source in sources))
        await pipeline.retry_notifications()
        finished=datetime.now(timezone.utc)
        checked=sum(x.get('discovered',0) for x in outcomes)
        new=sum(x.get('new',0) for x in outcomes)
        filtered=sum(x.get('filtered',0) for x in outcomes)
        state={
            'status':'running','phase':'complete','last_poll_at':iso(finished),
            'next_poll_at':scheduled_next if manual else iso(finished+timedelta(seconds=cfg.poll_interval_seconds)),
            'jobs_checked':checked,'new_recent_jobs':new,'alerts_sent':pipeline._cycle_notifications,
            'duplicates_ignored':sum(x.get('duplicates_removed',0) for x in outcomes),
            'sources_loaded':len(sources),
            'sources_attempted':len(sources),
            'sources_successful':sum(1 for x in outcomes if x.get('success')),
            'pages_fetched':sum(x.get('pages_fetched',0) for x in outcomes),
            'raw_jobs_discovered':sum(x.get('raw_jobs_discovered',x.get('discovered',0)) for x in outcomes),
            'normalized_jobs':sum(x.get('normalized_jobs',0) for x in outcomes),
            'jobs_0_90':sum(x.get('jobs_0_90',0) for x in outcomes),
            'computer_related_jobs':sum(x.get('computer_related',0) for x in outcomes),
            'entry_level_compatible':sum(x.get('entry_level_compatible',0) for x in outcomes),
            'new_database_records':new,
            'qualifying_alerts':pipeline._cycle_notifications,
        }
        # status update is best-effort: a webhook outage never stops polling.
        state.update({'sources_working':sum(1 for source in sources if repo.health(source.name).status=='healthy'),'linkedin_status':'READY' if cfg.brightdata_enabled and cfg.brightdata_api_token and cfg.brightdata_linkedin_jobs_dataset_id and cfg.brightdata_inputs('linkedin_jobs') else 'DISABLED','jobstreet_status':jobstreet_status(cfg,repo),'jobstreet_brightdata_status':brightdata_jobstreet_status(cfg)})
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
    repo.initialize_runtime_config(cfg); repo.expire_stale_jobs()
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

@app.get('/connect/jobstreet/{token}', response_class=HTMLResponse)
def jobstreet_connect_page(token: str):
    request = request_for_token(repo, token)
    if not request:
        request = request_for_token(repo, token, include_used=True)
    if not request:
        raise HTTPException(403, 'invalid, expired, or already-used connection link')
    session = interactive_session_for_request(request)
    if session and session.live_url:
        return HTMLResponse(_jobstreet_live_page(token, session.live_url, session.status, session.error))
    if request.used_at:
        if request.status == "COMPLETE":
            return HTMLResponse(_jobstreet_complete_page("JobStreet is connected and ready."))
        if request.status == "ERROR":
            return HTMLResponse(_jobstreet_complete_page("The private browser session did not complete. Start a new connection from Discord."))
        return HTMLResponse(_jobstreet_complete_page("Your private browser session is starting. Refresh this page in a moment."))
    state = 'Ready to start a private browser session.' if browserless_ready(cfg) else 'Browserless is not configured yet. Ask the administrator to add the required private service secrets.'
    return HTMLResponse(
        '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Connect JobStreet</title><main style="max-width:42rem;margin:3rem auto;font:16px system-ui;color:#222">'
        '<h1>Connect JobStreet</h1><p>Start a private browser session to connect JobStreet.</p>'
        '<p>You will sign in directly inside a temporary browser session. This service never asks for your Google password, 2FA code, or CAPTCHA response.</p>'
        f'<p><strong>Status:</strong> {escape(state)}</p>'
        '<p>This short-lived link is private. Do not share it.</p>'
        '<form method="post"><button type="submit" '
        f'{"disabled" if not browserless_ready(cfg) else ""}>START PRIVATE BROWSER</button></form></main>'
    )

def _jobstreet_complete_page(message: str) -> str:
    return (
        '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>JobStreet connection</title><main style="max-width:42rem;margin:3rem auto;font:16px system-ui;color:#222">'
        f'<h1>JobStreet connection</h1><p>{escape(message)}</p>'
        '<p>You can close this tab and return to Discord.</p></main>'
    )

def _jobstreet_live_page(token: str, live_url: str, status: str, error: str | None = None, auto_redirect: bool = False) -> str:
    if status == "READY":
        return _jobstreet_complete_page("JobStreet is connected and ready.")
    if status == "ERROR":
        return _jobstreet_complete_page(error or "The private browser session did not complete.")
    redirect = (
        f'<meta http-equiv="refresh" content="0;url={escape(live_url, quote=True)}">'
        f'<script>window.location.replace({json.dumps(live_url)});</script>'
        if auto_redirect and live_url else ''
    )
    handoff = (
        '<p>Redirecting to the live private browser now. If it does not open, '
        f'<a href="{escape(live_url, quote=True)}" target="_blank" rel="noopener noreferrer">open it here</a>.</p>'
        if auto_redirect and live_url
        else '<p><a href="' + escape(live_url, quote=True) + '" target="_blank" rel="noopener noreferrer">OPEN PRIVATE BROWSER</a></p>'
    )
    return (
        '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
        + redirect +
        '<title>Connect JobStreet</title><main style="max-width:42rem;margin:3rem auto;font:16px system-ui;color:#222">'
        '<h1>Connect JobStreet</h1><p>Complete JobStreet authentication in the private browser.</p>'
        '<p>Use Continue with Google, and complete any 2FA, security prompts, consent, or CAPTCHA yourself. '
        'The service never receives your Google password or challenge answers.</p>'
        + handoff +
        '<p>When authentication is complete, close the Browserless browser tab. This page will update automatically.</p>'
        f'<p id="status">Status: {escape(status)}</p>'
        f'''<script>
        const poll = async () => {{
          try {{
            const response = await fetch('/connect/jobstreet/{escape(token, quote=True)}/status');
            const data = await response.json();
            document.getElementById('status').textContent = 'Status: ' + data.status;
            if (data.status === 'READY' || data.status === 'ERROR') {{
              window.location.reload();
              return;
            }}
          }} catch (_) {{}}
          setTimeout(poll, 2000);
        }};
        poll();
        </script></main>'''
    )

@app.post('/connect/jobstreet/{token}', response_class=HTMLResponse)
async def start_jobstreet_connection(token: str):
    if not browserless_ready(cfg):
        raise HTTPException(503, 'JobStreet connection is not configured on this service')
    request = request_for_token(repo, token, consume=True)
    if not request:
        raise HTTPException(403, 'invalid, expired, or already-used connection link')
    session = await start_interactive_session(cfg, repo, request)
    if session.status == "ERROR":
        return HTMLResponse(_jobstreet_complete_page(session.error or "The private browser session did not complete."), status_code=502)
    return HTMLResponse(_jobstreet_live_page(token, session.live_url or "", session.status, session.error, auto_redirect=True))

@app.get('/connect/jobstreet/{token}/status')
def jobstreet_connection_status(token: str):
    request = request_for_token(repo, token, include_used=True)
    if not request:
        raise HTTPException(403, 'invalid, expired, or already-used connection link')
    session = interactive_session_for_request(request)
    status = session.status if session else request.status
    return {'status': status, 'error': session.error if session else None}

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

@app.get('/jobs/table',response_class=HTMLResponse)
def jobs_table(min_score:int=0, days:int=30, page:int=1, status:str|None=None):
    """Public, read-only table of the current stored job matches.

    `jobs_checked` is a scan counter; this table deliberately shows only jobs
    that passed normalization/filtering and were saved for review.
    """
    min_score=max(0,min(100,min_score)); days=max(1,min(90,days)); page=max(1,page); page_size=50
    cutoff=datetime.now(timezone.utc)-timedelta(days=days)
    with repo.sessions() as s:
        filters=[Job.status!=JobStatus.EXPIRED.value,Job.score>=min_score,Job.date_posted.is_not(None),Job.date_posted>=cutoff]
        if status: filters.append(Job.status==status.upper())
        query=(select(Job).where(*filters)
               .order_by(Job.date_posted.desc()).offset((page-1)*page_size).limit(page_size))
        rows=s.scalars(query).all()
        totals=dict(s.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    def posted(value):
        if not value: return 'Date unavailable'
        if value.tzinfo is None: value=value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime('%Y-%m-%d')
    def listing(url):
        if not url.startswith(('https://','http://')): return 'Unavailable'
        return f'<a class="view" href="{escape(url,quote=True)}" target="_blank" rel="noopener noreferrer">VIEW JOB</a>'
    table_rows=''.join(
        f'<tr><td>{posted(job.date_posted)}</td><td><a class="role" href="/jobs/{job.id}/timeline"><strong>{escape(job.title)}</strong></a><br><span>{escape(job.company)}</span></td><td>{escape(job.location or "Not stated")}</td><td>{job.score}%</td><td>{escape(job.status)}</td><td>{listing(job.application_url or job.url)}</td></tr>'
        for job in rows
    ) or '<tr><td colspan="6" class="empty">No recent stored matches meet these filters yet.</td></tr>'
    query_args=f'min_score={min_score}&days={days}' + (f'&status={escape(status,quote=True)}' if status else '')
    next_link=f'<a href="/jobs/table?{query_args}&page={page+1}">Next page →</a>' if len(rows)==page_size else ''
    status_chips=' '.join(f'<a class="chip" href="/jobs/table?min_score={min_score}&days={days}&status={key}">{escape(key.title())}: {value}</a>' for key,value in sorted(totals.items()))
    return HTMLResponse(f'''<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>After Hours Job Hunter — Jobs</title><style>
body{{margin:0;background:#130606;color:#f7eee8;font:15px system-ui,-apple-system,Segoe UI,sans-serif}}main{{max-width:1180px;margin:auto;padding:28px 18px}}h1{{margin:0;color:#ff7f00;font-size:24px}}p{{color:#cbbdb4}}.meta{{display:flex;gap:10px;flex-wrap:wrap;margin:20px 0}}.chip{{background:#2a1714;border:1px solid #5b2c20;padding:7px 10px;border-radius:6px;text-decoration:none;color:#f7eee8}}table{{width:100%;border-collapse:collapse;background:#21100e;border-left:4px solid #ff7f00}}th,td{{padding:13px 12px;text-align:left;border-bottom:1px solid #48231c;vertical-align:top}}th{{color:#ffb36b;font-size:12px;letter-spacing:.06em}}td span{{color:#cbbdb4;font-size:13px}}.view{{color:#fff;background:#b94d10;text-decoration:none;padding:7px 9px;border-radius:4px;font-size:12px;font-weight:700}}.role{{color:#f7eee8;text-decoration:none}}.empty{{color:#cbbdb4;text-align:center;padding:32px}}a{{color:#ffb36b}}@media(max-width:720px){{main{{padding:18px 10px}}th:nth-child(3),td:nth-child(3),th:nth-child(5),td:nth-child(5){{display:none}}th,td{{padding:11px 8px}}}}</style></head><body><main><h1>AFTER HOURS JOB HUNTER</h1><p>Recent stored job matches, ordered by the employer’s posted date. This page is read-only and never submits an application.</p><div class="meta"><span class="chip">Last {days} days</span><span class="chip">Minimum match: {min_score}%</span><span class="chip">Page {page}</span><a class="chip" href="/jobs/table?min_score=60&days=30">60%+ matches</a><a class="chip" href="/jobs/export.csv?min_score={min_score}&days={days}">DOWNLOAD CSV</a>{status_chips}</div><table><thead><tr><th>POSTED</th><th>ROLE / COMPANY</th><th>LOCATION</th><th>MATCH</th><th>STATUS</th><th>LISTING</th></tr></thead><tbody>{table_rows}</tbody></table><p>{next_link}</p><p>After Hours Job Hunter • Made by masoncalix</p></main></body></html>''')

@app.get('/jobs/export.csv')
def export_jobs_csv(min_score:int=0, days:int=30):
    cutoff=datetime.now(timezone.utc)-timedelta(days=max(1,min(90,days)))
    with repo.sessions() as s:
        rows=s.scalars(select(Job).where(Job.status!=JobStatus.EXPIRED.value,Job.score>=max(0,min(100,min_score)),Job.date_posted.is_not(None),Job.date_posted>=cutoff).order_by(Job.date_posted.desc())).all()
    output=io.StringIO(); writer=csv.writer(output); writer.writerow(['posted_date','title','company','location','score','status','source','job_url'])
    writer.writerows([(job.date_posted.isoformat() if job.date_posted else '',job.title,job.company,job.location,job.score,job.status,job.source,job.application_url or job.url) for job in rows])
    return Response(output.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="after-hours-job-board.csv"'})

@app.get('/jobs/{job_id}/timeline',response_class=HTMLResponse)
def job_timeline(job_id:int):
    with repo.sessions() as s:
        job=s.get(Job,job_id)
        if not job: raise HTTPException(404,'job not found')
        events=s.scalars(select(JobEvent).where(JobEvent.job_id==job_id).order_by(JobEvent.created_at.desc())).all()
    entries=''.join(f'<li><strong>{escape(event.event_type.replace("_"," ").title())}</strong><br><span>{escape(event.detail)} · {escape(event.created_at.strftime("%Y-%m-%d %H:%M UTC"))}</span></li>' for event in events) or '<li>No application activity has been recorded yet.</li>'
    return HTMLResponse(f'<!doctype html><title>Application Timeline</title><style>body{{background:#130606;color:#f7eee8;font:16px system-ui;padding:28px}}h1{{color:#ff7f00}}li{{background:#21100e;border-left:4px solid #ff7f00;margin:10px 0;padding:12px}}span{{color:#cbbdb4}}</style><h1>{escape(job.title)}</h1><p>{escape(job.company)} · Current status: {escape(job.status)}</p><h2>Application activity</h2><ul>{entries}</ul><p><a href="/jobs/table">Back to job board</a></p>')
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
        repo.set_job_status(job.id,status,f'{status.title()} from signed action.')
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
