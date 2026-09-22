from __future__ import annotations
import asyncio, csv, hmac, io, json, logging, os, random, socket, uuid
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlencode
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, File, Header, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from .config import settings
from .jobs import NormalizedJob
from .models import Job, JobStatus, SourceRun, SourceHealth, JobEvent
from .jobstreet_link import (
    ConnectionError,
    complete_request,
    request_for_token,
    windows_connector_cmd,
    windows_connector_ps1,
    windows_connector_python,
)
from .services import Pipeline, Repository
from .sources import configured_sources
from .applications import write_package, generate_cover_letter, cover_letter_metadata
from .ai import gemini
from .security import ActionTokens
from .brightdata import brightdata_sources
from .jobstreet import brightdata_jobstreet_status, jobstreet_sources, jobstreet_status
from .jobspy_source import jobspy_provider_status, jobspy_sources
from .discovery import discover_sources
from .manual_search import ManualJobSearch
from .discord_bot import run_discord_bot
from .resumes import extract_resume_text
from .logging_config import configure_logging
cfg=settings(); configure_logging(cfg.log_level); repo=Repository(cfg.database_url); pipeline=Pipeline(repo,cfg)
manual_search=ManualJobSearch(cfg,repo)
poll_lock=asyncio.Lock()
cover_letter_locks: dict[int, asyncio.Lock] = {}
worker_instance_id=f'{os.getenv("RENDER_SERVICE_ID") or socket.gethostname()}-{uuid.uuid4().hex[:10]}'
def iso(value): return value.astimezone(timezone.utc).isoformat()
def scheduler_snapshot():
    saved=repo.state('scheduler',{}) or {}
    with repo.sessions() as s:
        source_rows=s.scalars(select(SourceHealth)).all()
        recent=s.scalars(select(Job.id).where(Job.date_posted>=datetime.now(timezone.utc)-timedelta(days=7),Job.status!=JobStatus.EXPIRED.value)).all()
    health_rows={row.source:row for row in source_rows}
    next_poll=saved.get('next_poll_at'); seconds=0
    if next_poll:
        try: seconds=max(0,int((datetime.fromisoformat(next_poll)-datetime.now(timezone.utc)).total_seconds()))
        except ValueError: pass
    heartbeat=(saved.get('worker_heartbeat_at') or (repo.state('scheduler_heartbeat',{}) or {}).get('at')); heartbeat_age=None
    if heartbeat:
        try: heartbeat_age=max(0,int((datetime.now(timezone.utc)-datetime.fromisoformat(heartbeat)).total_seconds()))
        except ValueError: heartbeat_age=None
    worker_status='HEALTHY' if heartbeat_age is not None and heartbeat_age <= cfg.worker_heartbeat_timeout_seconds else 'STALE'
    saved.update({'service_status':'online','status':saved.get('status','starting'),'worker_status':worker_status,'worker_heartbeat_at':heartbeat,'worker_heartbeat_age_seconds':heartbeat_age,'last_poll_at':saved.get('last_poll_at'),'next_poll_at':next_poll,'seconds_until_next_poll':seconds,'sources_working':sum(x.status=='healthy' for x in source_rows),'recent_jobs_found':len(recent),'discord_status':'READY' if cfg.discord_bot_token or cfg.discord_webhook_url else 'DISABLED','database_status':'CONNECTED','linkedin_status':'READY' if cfg.brightdata_enabled and cfg.brightdata_api_token and cfg.brightdata_linkedin_jobs_dataset_id and cfg.brightdata_inputs('linkedin_jobs') else 'DISABLED','indeed_status':jobspy_provider_status(cfg,health_rows,'indeed'),'google_jobs_status':jobspy_provider_status(cfg,health_rows,'google'),'jobstreet_status':jobstreet_status(cfg,repo),'jobstreet_brightdata_status':brightdata_jobstreet_status(cfg),'source_registry_count':len(repo.source_registry_snapshot())})
    return saved

def _merged_source_targets():
    configured=cfg.source_targets
    for target in configured:
        repo.upsert_source_target(target,'configured')
    merged=[]; seen=set()
    for target in configured + repo.registry_targets():
        key=(target.get('kind'),target.get('token') or target.get('site') or target.get('board') or target.get('company'))
        if key in seen: continue
        seen.add(key); merged.append(target)
    return merged
async def poll_once(manual=False):
    async with poll_lock:
        started=datetime.now(timezone.utc)
        previous=repo.state('scheduler',{}) or {}
        if not manual:
            # A manual scan may finish just after the automatic deadline while
            # this coroutine is waiting on ``poll_lock``. Do not immediately
            # run the same full batch twice and spend another set of source
            # requests; advance the normal cadence from the completed scan.
            try:
                deadline=datetime.fromisoformat(previous.get('next_poll_at',''))
                last_scan=datetime.fromisoformat(previous.get('last_poll_at',''))
                if deadline.tzinfo is None: deadline=deadline.replace(tzinfo=timezone.utc)
                if last_scan.tzinfo is None: last_scan=last_scan.replace(tzinfo=timezone.utc)
            except (TypeError,ValueError):
                deadline=last_scan=None
            if deadline and last_scan and deadline <= started and last_scan >= deadline and started-last_scan <= timedelta(minutes=5):
                skipped={**previous,'status':'running','phase':'complete','next_poll_at':iso(started+timedelta(seconds=cfg.poll_interval_seconds))}
                repo.set_state('scheduler',skipped)
                return []
        scheduled_next=previous.get('next_poll_at') if manual else None
        if not scheduled_next: scheduled_next=iso(started+timedelta(seconds=cfg.poll_interval_seconds))
        repo.set_state('scheduler',{'status':'running','phase':'scanning','last_poll_at':iso(started),'next_poll_at':scheduled_next,'jobs_checked':0,'new_recent_jobs':0,'alerts_sent':0})
        if not cfg.discord_bot_token:
            try: await pipeline.discord.update_status(repo,scheduler_snapshot())
            except Exception as exc: logging.warning('discord_control_panel_update_failed',extra={'error':str(exc)})
        try:
            pipeline.begin_cycle()
            discovery_state=repo.state('source_discovery',{}) or {}
            should_discover=cfg.source_discovery_enabled
            if discovery_state.get('completed_at'):
                try: should_discover=(datetime.now(timezone.utc)-datetime.fromisoformat(discovery_state['completed_at'])).total_seconds() >= cfg.source_discovery_interval_seconds
                except ValueError: should_discover=True
            if should_discover:
                repo.set_state('source_discovery',{'status':'running','started_at':iso(datetime.now(timezone.utc))})
                try:
                    discovered=await discover_sources(cfg,repo)
                    repo.set_state('source_discovery',{'status':'healthy','completed_at':iso(datetime.now(timezone.utc)),'sources_discovered':len(discovered)})
                except Exception as exc:
                    repo.set_state('source_discovery',{'status':'degraded','completed_at':iso(datetime.now(timezone.utc)),'error':str(exc)})
                    logging.warning('source_discovery_failed',extra={'error':str(exc)})
            public_sources=configured_sources(_merged_source_targets())
            sources=public_sources+brightdata_sources(cfg,repo)+jobspy_sources(cfg,repo,force=manual)+jobstreet_sources(cfg,repo)
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
                    else getattr(source, 'timeout_seconds', cfg.scan_source_timeout_seconds)
                    if source.name.startswith('jobspy:')
                    else cfg.scan_source_timeout_seconds
                )
                try: return await asyncio.wait_for(pipeline.run_source(source),timeout=timeout)
                except asyncio.TimeoutError:
                    return {'source':source.name,'discovered':0,'new':0,'filtered':0,'timeout':True,'success':False,'pages_fetched':0}
                except Exception as exc:
                    repo.health_failure(source.name,f'unhandled source error: {type(exc).__name__}')
                    logging.exception('source_isolation_failed',extra={'source':source.name})
                    return {'source':source.name,'discovered':0,'new':0,'filtered':0,'success':False,'error':type(exc).__name__,'pages_fetched':0}
        outcomes=list(await asyncio.gather(*(limited(source) for source in sources), return_exceptions=False))
        await pipeline.retry_notifications()
        digest_sender=getattr(pipeline,'send_digest',None)
        digest_sent=await digest_sender() if digest_sender else 0
        finished=datetime.now(timezone.utc)
        checked=sum(x.get('discovered',0) for x in outcomes)
        new=sum(x.get('new',0) for x in outcomes)
        filtered=sum(x.get('filtered',0) for x in outcomes)
        state={
            'status':'running','phase':'complete','last_poll_at':iso(finished),
            'next_poll_at':scheduled_next if manual else iso(finished+timedelta(seconds=cfg.poll_interval_seconds)),
            'jobs_checked':checked,'new_recent_jobs':new,'alerts_sent':getattr(pipeline,'_cycle_delivered',0) + digest_sent,
            'alerts_queued':pipeline._cycle_notifications,
            'duplicates_ignored':sum(x.get('duplicates_removed',0) for x in outcomes),
            'sources_loaded':len(sources),
            'sources_attempted':len(sources),
            'sources_successful':sum(1 for x in outcomes if x.get('success')),
            'pages_fetched':sum(x.get('pages_fetched',0) for x in outcomes),
            'raw_jobs_discovered':sum(x.get('raw_jobs_discovered',x.get('discovered',0)) for x in outcomes),
            'normalized_jobs':sum(x.get('normalized_jobs',0) for x in outcomes),
            'jobs_0_90':sum(x.get('jobs_0_90',0) for x in outcomes),
            'ph_remote_ph_jobs':sum(x.get('ph_remote_ph',0) for x in outcomes),
            'computer_related_jobs':sum(x.get('computer_related',0) for x in outcomes),
            'entry_level_compatible':sum(x.get('entry_level_compatible',0) for x in outcomes),
            'qualifying_jobs':sum(x.get('qualifying',0) for x in outcomes),
            'malformed_jobs_skipped':sum(x.get('malformed_jobs',0) for x in outcomes),
            'new_database_records':new,
            'updated_database_records':sum(x.get('jobs_updated',0) for x in outcomes),
            'qualifying_alerts':pipeline._cycle_notifications,
            'source_funnels':{
                item['source']:{
                    'raw':item.get('raw_jobs_discovered',0),
                    'normalized':item.get('normalized_jobs',0),
                    '0_90_days':item.get('jobs_0_90',0),
                    'ph_remote_ph':item.get('ph_remote_ph',0),
                    'tech_related':item.get('computer_related',0),
                    'entry_level_compatible':item.get('entry_level_compatible',0),
                    'duplicates':item.get('duplicates_removed',0),
                    'new_unique':item.get('new',0),
                    'qualifying':item.get('qualifying',0),
                }
                for item in outcomes if item['source'].startswith('jobspy:')
            },
        }
        # status update is best-effort: a webhook outage never stops polling.
        def load_current_health():
            with repo.sessions() as session:
                return {row.source:row for row in session.scalars(select(SourceHealth)).all()}
        current_health=await asyncio.to_thread(load_current_health)
        state.update({'sources_working':sum(1 for source in sources if repo.health(source.name).status=='healthy'),'linkedin_status':'READY' if cfg.brightdata_enabled and cfg.brightdata_api_token and cfg.brightdata_linkedin_jobs_dataset_id and cfg.brightdata_inputs('linkedin_jobs') else 'DISABLED','indeed_status':jobspy_provider_status(cfg,current_health,'indeed'),'google_jobs_status':jobspy_provider_status(cfg,current_health,'google'),'jobstreet_status':jobstreet_status(cfg,repo),'jobstreet_brightdata_status':brightdata_jobstreet_status(cfg)})
        repo.set_state('scheduler',state)
        if not cfg.discord_bot_token:
            try: await pipeline.discord.update_status(repo,scheduler_snapshot())
            except Exception as exc: logging.warning('discord_status_update_failed',extra={'error':str(exc)})
        return outcomes
async def worker():
    acquired=False
    try:
        while True:
            if not acquired:
                acquired=await asyncio.to_thread(repo.acquire_worker_lease,worker_instance_id,cfg.worker_heartbeat_timeout_seconds)
                if not acquired:
                    await asyncio.sleep(min(60,cfg.poll_interval_seconds)); continue
            await asyncio.to_thread(repo.set_state,'scheduler_heartbeat',{'at':iso(datetime.now(timezone.utc)),'instance_id':worker_instance_id})
            try:
                await poll_once()
            except Exception:
                logging.exception('automatic_poll_failed')
            if not await asyncio.to_thread(repo.renew_worker_lease,worker_instance_id,cfg.worker_heartbeat_timeout_seconds):
                logging.warning('discovery_worker_lease_lost')
                acquired=False
                continue
            state=repo.state('scheduler',{}) or {}
            next_poll=state.get('next_poll_at')
            try:
                delay=max(0,(datetime.fromisoformat(next_poll)-datetime.now(timezone.utc)).total_seconds()) if next_poll else cfg.poll_interval_seconds
            except (TypeError, ValueError):
                delay=cfg.poll_interval_seconds
            delay += random.uniform(0,max(0,cfg.scheduler_jitter_seconds))
            # Keep both the durable lease and observable heartbeat alive during
            # the normal 15-minute wait. A one-shot sleep previously let the
            # 120-second lease expire, allowing a second worker to begin the
            # same cycle before the first process woke up.
            remaining=delay
            while remaining>0 and acquired:
                step=min(30,remaining)
                await asyncio.sleep(step)
                remaining-=step
                await asyncio.to_thread(repo.set_state,'scheduler_heartbeat',{'at':iso(datetime.now(timezone.utc)),'instance_id':worker_instance_id})
                if not await asyncio.to_thread(repo.renew_worker_lease,worker_instance_id,cfg.worker_heartbeat_timeout_seconds):
                    logging.warning('discovery_worker_lease_lost_during_wait')
                    acquired=False
    finally:
        if acquired:
            await asyncio.to_thread(repo.release_worker_lease,worker_instance_id)
def discord_task_done(task):
    if task.cancelled(): return
    error=task.exception()
    if error:
        logging.error('discord_bot_task_failed',extra={'error':str(error)},exc_info=(type(error),error,error.__traceback__))
    else:
        logging.warning('discord_bot_stopped')


async def discord_worker():
    """Keep the Discord gateway available without ever stopping job polling.

    Discord may close a gateway connection transiently.  The bot client marks
    itself unhealthy on exit; this supervisor retries with a bounded backoff
    while the FastAPI lifespan remains active.  Cancellation during shutdown
    is deliberately propagated so it never creates a zombie reconnect loop.
    """
    retry_delay = 5
    while True:
        try:
            await run_discord_bot(cfg, repo, manual_search, scheduler_snapshot, manual_scan)
            logging.warning('discord_bot_stopped; reconnecting')
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception('discord_bot_task_failed; reconnecting')
        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 60)
@asynccontextmanager
async def lifespan(app):
    repo.initialize_runtime_config(cfg); repo.expire_stale_jobs()
    try:
        if cfg.discord_bot_token: await pipeline.discord.remove_webhook_control_panel(repo)
        else: await pipeline.discord.update_status(repo,scheduler_snapshot())
    except Exception as exc: logging.warning('discord_control_panel_update_failed',extra={'error':str(exc)})
    task=asyncio.create_task(worker()) if cfg.polling_enabled else None
    # In a combined local process polling and the gateway run together. In the
    # split Render deployment, the always-on worker owns both; the web process
    # stays API-only and cannot strand the gateway when it sleeps/restarts.
    bot_task=asyncio.create_task(discord_worker()) if cfg.discord_bot_token and cfg.polling_enabled else None
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
    request = request_for_token(repo, token, app_secret_key=cfg.app_secret_key)
    if not request:
        request = request_for_token(
            repo, token, include_used=True, app_secret_key=cfg.app_secret_key
        )
    if not request:
        raise HTTPException(403, 'invalid, expired, or already-used connection link')
    if request.status == "COMPLETE":
        return HTMLResponse(
            '<!doctype html><title>JobStreet ready</title><main>'
            '<h1>JobStreet</h1><p>READY</p>'
            '<p>The encrypted session is saved. You can close this tab.</p></main>'
        )
    base = cfg.public_base_url or ""
    return HTMLResponse(
        '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Connect JobStreet</title><main style="max-width:42rem;margin:3rem auto;font:16px system-ui;color:#222">'
        '<h1>CONNECT JOBSTREET</h1>'
        '<p>Step 1: download the Windows connector.</p>'
        '<p>Step 2: run it on the Windows PC where you want to authenticate.</p>'
        '<p>Step 3: the connector opens your installed Google Chrome automatically with a separate '
        'temporary profile. Complete JobStreet/Google login, 2FA, security prompts, and CAPTCHA yourself.</p>'
        '<p>Playwright only attaches to that Chrome window over a local connection. Your normal Chrome '
        'profile is not used, and no browser download is required.</p>'
        '<p><strong>Status:</strong> <span id="status">WAITING FOR USER LOGIN</span></p>'
        f'<p><a download href="{escape(base + "/connect/jobstreet/" + token + "/connector.ps1", quote=True)}">'
        '<button>DOWNLOAD WINDOWS CONNECTOR</button></a></p>'
        f'<p><a download href="{escape(base + "/connect/jobstreet/" + token + "/connector.cmd", quote=True)}">'
        '<button>DOWNLOAD COMMAND CONNECTOR</button></a></p>'
        '<p>This private setup link and its connector token expire after one use or ten minutes. '
        'Never share them.</p>'
        f'''<script>
        const poll = async () => {{
          try {{
            const response = await fetch('/connect/jobstreet/{escape(token, quote=True)}/status');
            const data = await response.json();
            document.getElementById('status').textContent = data.status;
            if (data.status === 'READY') {{
              document.querySelector('main').innerHTML = '<h1>JobStreet</h1><p>READY</p><p>You can close this tab.</p>';
              return;
            }}
          }} catch (_) {{}}
          setTimeout(poll, 2000);
        }};
        poll();
        </script></main>'''
    )


def _valid_connector_request(token: str):
    request = request_for_token(repo, token, app_secret_key=cfg.app_secret_key)
    if not request:
        raise HTTPException(403, 'invalid, expired, or already-used connection link')
    return request


@app.get('/connect/jobstreet/{token}/connector.ps1')
def download_jobstreet_connector(token: str):
    _valid_connector_request(token)
    if not cfg.public_base_url:
        raise HTTPException(503, 'PUBLIC_BASE_URL is required for the Windows connector')
    from .jobstreet_link import windows_connector_ps1
    return Response(
        content=windows_connector_ps1(cfg.public_base_url, token, cfg),
        media_type='text/plain',
        headers={'Content-Disposition': 'attachment; filename="jobstreet-connector.ps1"'},
    )


@app.get('/connect/jobstreet/{token}/connector.cmd')
def download_jobstreet_cmd(token: str):
    _valid_connector_request(token)
    if not cfg.public_base_url:
        raise HTTPException(503, 'PUBLIC_BASE_URL is required for the Windows connector')
    from .jobstreet_link import windows_connector_cmd
    return Response(
        content=windows_connector_cmd(cfg.public_base_url, token),
        media_type='text/plain',
        headers={'Content-Disposition': 'attachment; filename="jobstreet-connector.cmd"'},
    )


@app.get('/connect/jobstreet/{token}/connector.py')
def download_jobstreet_python_helper(token: str):
    _valid_connector_request(token)
    return Response(content=windows_connector_python(), media_type='text/x-python')


@app.post('/connect/jobstreet/{token}/session')
def upload_jobstreet_session(
    token: str,
    storage_state: dict,
    x_jobstreet_connection_token: str | None = Header(default=None),
):
    if not x_jobstreet_connection_token or not hmac.compare_digest(
        token, x_jobstreet_connection_token
    ):
        raise HTTPException(403, 'invalid JobStreet connection token')
    try:
        status = complete_request(cfg, repo, token, storage_state)
    except ConnectionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, 'connection storage is temporarily unavailable') from exc
    return {'status': status}


@app.get('/connect/jobstreet/{token}/status')
def jobstreet_connection_status(token: str):
    request = request_for_token(
        repo, token, include_used=True, app_secret_key=cfg.app_secret_key
    )
    if not request:
        raise HTTPException(403, 'invalid, expired, or already-used connection link')
    return {'status': 'READY' if request.status == 'COMPLETE' else 'WAITING FOR USER LOGIN'}

@app.get('/resume',response_class=HTMLResponse)
def resume_status_page():
    raise HTTPException(404,'resume management is available through a signed private Discord link')

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
    scheduler=scheduler_snapshot(); failing=sum(x.status in {'unhealthy','degraded'} for x in sources); healthy=sum(x.status=='healthy' for x in sources)
    notification_health={'configured':bool(cfg.discord_bot_token or cfg.discord_webhook_url),'status':'READY' if cfg.discord_bot_token or cfg.discord_webhook_url else 'DISABLED'}
    state='ok' if (not cfg.worker_required or scheduler.get('worker_status')=='HEALTHY') else 'degraded'
    return {'status':state,'database':'ok','worker':scheduler.get('worker_status'),'discord_configured':notification_health['configured'],'discord_bot':repo.state('discord_bot_health',{'healthy':False}),'secure_actions_configured':bool(cfg.app_secret_key and cfg.public_base_url),'gmail_configured':bool(cfg.google_client_id and cfg.google_client_secret),'notifications':notification_health,'scheduler':scheduler,'source_summary':{'total':len(sources),'healthy':healthy,'failing':failing},'sources':{x.source:{'status':x.status,'jobs':x.last_job_count,'last_success':x.last_success_at,'consecutive_failures':x.consecutive_failures,'health_score':x.health_score,'last_error':x.last_error} for x in sources},'source_registry':repo.source_registry_snapshot(),'ai':gemini().health()}

@app.get('/live')
def live():
    return {'status':'ok','service':'job-discovery'}

@app.get('/ready')
def ready():
    payload=health()
    if payload['status'] != 'ok':
        raise HTTPException(503,detail=payload)
    return payload

@app.get('/metrics')
def metrics():
    now=datetime.now(timezone.utc); hour=now-timedelta(hours=1); day=now-timedelta(days=1)
    with repo.sessions() as s:
        discovered_1h=s.scalar(select(func.count()).select_from(Job).where(Job.first_seen_at>=hour)) or 0
        discovered_24h=s.scalar(select(func.count()).select_from(Job).where(Job.first_seen_at>=day)) or 0
        matched_24h=s.scalar(select(func.count()).select_from(Job).where(Job.first_seen_at>=day,Job.score>=cfg.min_notify_score)) or 0
        alerts_sent=s.scalar(select(func.count()).select_from(Job).where(Job.notification_state=='SENT',Job.last_notification_attempt>=day)) or 0
        alerts_failed=s.scalar(select(func.count()).select_from(Job).where(Job.notification_state=='FAILED')) or 0
    scheduler=scheduler_snapshot()
    return {'api':'ok','database':'ok','worker':scheduler.get('worker_status'),'scheduler':scheduler,'sources':{'active':scheduler.get('sources_working',0),'failing':sum(1 for value in health()['sources'].values() if value['status'] in {'unhealthy','degraded'}),'registry':len(repo.source_registry_snapshot())},'jobs':{'discovered_1h':discovered_1h,'discovered_24h':discovered_24h,'matched_24h':matched_24h},'notifications':{'sent_24h':alerts_sent,'failed':alerts_failed}}
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


def _public_job(job: Job) -> dict:
    """Expose job facts without private notes, emails, or generated letters."""
    return {
        'id':job.id,'source':job.source,'source_job_id':job.source_job_id,
        'title':job.title,'company':job.company,'location':job.location,
        'country':job.country,'work_setup':job.work_setup,'salary':job.salary,
        'date_posted':job.date_posted,'employment_type':job.employment_type,
        'seniority':job.seniority,'skills':job.skills,'category':job.category,
        'score':job.score,'match_reasons':job.match_reasons,'warnings':job.warnings,
        'url':job.url,'application_url':job.application_url,'is_active':job.is_active,
    }


@app.get('/jobs')
def jobs(min_score:int=0,status:str|None=None,include_expired:bool=False):
    with repo.sessions() as s:
        q=select(Job).where(Job.score>=min_score)
        if status: q=q.where(Job.status==status)
        elif not include_expired: q=q.where(Job.status!=JobStatus.EXPIRED.value)
        rows=s.scalars(q.order_by(Job.date_discovered.desc()).limit(200)).all()
        return [_public_job(job) for job in rows]

@app.get('/jobs/table',response_class=HTMLResponse)
def jobs_table(min_score:int=0, days:int=30, page:int=1, status:str|None=None, role:str='', technology:str='', location:str='', remote:bool=False, provider:str='', company:str=''):
    """Public, read-only table of the current stored job matches.

    `jobs_checked` is a scan counter; this table deliberately shows only jobs
    that passed normalization/filtering and were saved for review.
    """
    min_score=max(0,min(100,min_score)); days=max(1,min(90,days)); page=max(1,page); page_size=50
    cutoff=datetime.now(timezone.utc)-timedelta(days=days)
    with repo.sessions() as s:
        filters=[Job.status!=JobStatus.EXPIRED.value,Job.score>=min_score,Job.date_posted.is_not(None),Job.date_posted>=cutoff]
        if status: filters.append(Job.status==status.upper())
        if role.strip(): filters.append(Job.title.ilike(f'%{role.strip()}%'))
        if technology.strip(): filters.append(or_(Job.description.ilike(f'%{technology.strip()}%'),Job.requirements.ilike(f'%{technology.strip()}%')))
        if location.strip(): filters.append(Job.location.ilike(f'%{location.strip()}%'))
        if remote: filters.append(or_(Job.work_setup.ilike('%remote%'),Job.location.ilike('%remote%'),Job.location.ilike('%work from home%')))
        if provider.strip(): filters.append(Job.source.ilike(f'{provider.strip()}:%'))
        if company.strip(): filters.append(Job.company.ilike(f'%{company.strip()}%'))
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
    query_values={'min_score':min_score,'days':days,'role':role,'technology':technology,'location':location,'provider':provider,'company':company}
    if status: query_values['status']=status
    if remote: query_values['remote']='true'
    query_args=urlencode(query_values)
    next_link=f'<a href="/jobs/table?{query_args}&page={page+1}">Next page →</a>' if len(rows)==page_size else ''
    status_chips=' '.join(f'<a class="chip" href="/jobs/table?min_score={min_score}&days={days}&status={key}">{escape(key.title())}: {value}</a>' for key,value in sorted(totals.items()))
    filters_html=f'''<form method="get" style="display:flex;gap:8px;flex-wrap:wrap;margin:18px 0"><input name="role" placeholder="Role" value="{escape(role,quote=True)}"><input name="technology" placeholder="Technology" value="{escape(technology,quote=True)}"><input name="location" placeholder="Location" value="{escape(location,quote=True)}"><input name="provider" placeholder="Provider" value="{escape(provider,quote=True)}"><input name="company" placeholder="Company" value="{escape(company,quote=True)}"><input name="min_score" type="number" min="0" max="100" value="{min_score}" title="Minimum score"><input name="days" type="number" min="1" max="90" value="{days}" title="Posted within days"><label><input name="remote" type="checkbox" value="true" {'checked' if remote else ''}> Remote</label><button>FILTER</button></form>'''
    status_chips=filters_html+status_chips
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


async def canonical_cover_letter(job_id: int, regenerate: bool = False):
    """Persist one active cover-letter version before any legacy package export."""
    lock=cover_letter_locks.setdefault(job_id,asyncio.Lock())
    async with lock:
        def load():
            with repo.sessions() as s:
                return s.get(Job, job_id)
        job = await asyncio.to_thread(load)
        if not job:
            raise HTTPException(404, 'job not found')
        result = await generate_cover_letter(job, use_ai=True, regenerate=regenerate, resume_text=repo.resume_text())
        def save():
            with repo.sessions() as s:
                stored=s.get(Job, job_id)
                if not stored:
                    return None
                stored.raw_metadata=cover_letter_metadata(stored.raw_metadata, result)
                s.commit()
                return stored
        stored=await asyncio.to_thread(save)
        if not stored:
            raise HTTPException(404, 'job not found')
        return stored, result
@app.get('/latest')
def latest_jobs(limit:int=10):
    with repo.sessions() as s:
        cutoff=datetime.now(timezone.utc)-timedelta(days=90)
        rows=s.scalars(select(Job).where(
            Job.status!=JobStatus.EXPIRED.value,
            Job.score>=cfg.min_notify_score,
            Job.date_posted.is_not(None),
            Job.date_posted>=cutoff,
        ).order_by(Job.date_posted.desc(),Job.score.desc()).limit(max(1,min(limit,25)))).all()
        return [_public_job(job) for job in rows]
@app.patch('/jobs/{job_id}/status')
def update_status(job_id:int, update:StatusUpdate, x_admin_token: str | None = Header(default=None)):
    if not cfg.admin_api_token or not x_admin_token or not hmac.compare_digest(x_admin_token,cfg.admin_api_token):
        raise HTTPException(404,'status updates are available through signed Discord actions')
    with repo.sessions() as s:
        job=s.get(Job,job_id)
        if not job: raise HTTPException(404,'job not found')
        job.status=update.status.value; s.commit(); return {'id':job.id,'status':job.status}
@app.post('/jobs/{job_id}/prepare-application')
async def prepare_application(job_id:int):
    raise HTTPException(410,'application preparation is available through signed Discord actions')
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
        stored,result=await canonical_cover_letter(job.id,regenerate=True)
        resume=repo.resume_record()
        path=write_package(stored,letter=result.text,resume_bytes=resume['file_data'] if resume else None,resume_filename=resume['filename'] if resume else None)
        return {'result':'cover letter generated','generation_method':result.method,'path':str(path)}
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
    with repo.sessions() as s:
        rows=s.scalars(select(SourceRun).order_by(SourceRun.id.desc()).limit(100)).all()
        return [{'id':row.id,'source':row.source,'started_at':row.started_at,'completed_at':row.completed_at,'discovered':row.discovered,'new_jobs':row.new_jobs,'filtered':row.filtered,'jobs_updated':row.jobs_updated,'duplicates':row.duplicates,'duration_seconds':row.duration_seconds,'status':row.status,'success':row.success} for row in rows]
@app.post('/fixtures/smoke')
async def smoke(x_admin_token: str | None = Header(default=None)):
    if not cfg.enable_fixture_routes or not cfg.admin_api_token or not x_admin_token or not hmac.compare_digest(x_admin_token,cfg.admin_api_token):
        raise HTTPException(404,'fixture routes are disabled')
    item=NormalizedJob(source='fixture',source_job_id='junior-devops-001',title='Junior DevOps Engineer',company='Fixture Cloud Inc',location='Makati, Philippines — Hybrid',description='Fresh graduate role. AWS Docker Terraform Linux CI/CD GitHub Actions Kubernetes.',url='https://example.invalid/jobs/junior-devops-001')
    job, accepted=await pipeline.process(item)
    return {'accepted':accepted,'created':bool(job),'job_id':job.id if job else None}
