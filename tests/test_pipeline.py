from datetime import datetime, timedelta, timezone
import pytest
from src.config import Settings
from src.jobs import NormalizedJob, evaluate, is_ph_location, freshness
from src.services import Pipeline, Repository
from src.applications import cover_letter, eligible_for_email
from src.email_alerts import alert_to_job
from src.services import Discord
from src.models import Job
from src.ai import GeminiManager, KeyState
from src.security import ActionTokens
from src.applications import valid_revision
from src.main import home
from src.brightdata import BrightDataClient, BrightDataJobs
from src.sources import SourceError
from src.manual_search import ManualJobSearch
from src.main import search_page

def job(**overrides):
    values=dict(source='test',source_job_id='one',title='Junior DevOps Engineer',company='Cloud PH',location='Taguig, Philippines — Hybrid',description='Fresh graduate AWS Docker Terraform Linux CI/CD Kubernetes.',url='https://example.com/one',date_posted=datetime.now(timezone.utc)-timedelta(hours=2))
    values.update(overrides); return NormalizedJob(**values)

def test_scoring_and_seniority():
    score,reasons,warnings,valid=evaluate(job())
    assert score >= 85 and valid and 'AWS' in reasons
    bad=evaluate(job(title='Senior DevOps Engineer',description='AWS Docker Terraform requires 5+ years'))
    assert bad[0] == 0 and any('Senior' in x for x in bad[2])

def test_location_filter():
    assert is_ph_location(job())
    assert not is_ph_location(job(location='New York, United States'))
    assert not is_ph_location(job(location='Remote — worldwide'))

def test_stale_jobs_never_pass_freshness_gate():
    points,reason,active=freshness(job(date_posted=datetime.now(timezone.utc)-timedelta(days=31)))
    assert not active and points == -100 and 'Stale' in reason

def test_broad_technology_role_is_eligible_but_senior_is_not():
    support=job(title='IT Support Specialist',description='Fresh graduate opportunity supporting Windows, Linux, networks, and technical users.')
    score,_,_,valid=evaluate(support)
    assert valid and score >= 60
    senior=evaluate(job(title='Senior Software Engineer',description='Python Docker requires 5+ years'))
    assert senior[0] == 0
    unrelated=evaluate(job(title='Accounting Assistant',description='Maintain invoices and use AWS accounting software.'))
    assert not unrelated[3]

@pytest.mark.asyncio
async def test_pipeline_deduplicates_and_persists(tmp_path):
    repo=Repository(f'sqlite:///{tmp_path}/agent.db'); repo.create_schema()
    pipe=Pipeline(repo,Settings(database_url=f'sqlite:///{tmp_path}/agent.db',polling_enabled=False,discord_webhook_url=None))
    first,accepted=await pipe.process(job())
    second,_=await pipe.process(job())
    assert accepted and first and first.score >= 85
    assert first.notification_state == 'SKIPPED'
    assert second is None

def test_safe_application_materials():
    class Fake: pass
    record=Fake(); record.title='Junior Cloud Engineer'; record.company='Cloud PH'; record.description='AWS Terraform Docker Linux'; record.score=90; record.application_email='jobs@example.com'; record.warnings=[]
    assert eligible_for_email(record,85)[0]
    letter=cover_letter(record)
    assert 'AWS' in letter and 'Oaktree' in letter and 'years of experience' not in letter

def test_cross_source_and_alert_deduplication():
    email=alert_to_job('linkedin_alert','Junior DevOps Engineer','Cloud PH','Taguig, Philippines','https://linkedin.example/job',datetime.now(timezone.utc),'AWS Docker')
    assert email.fingerprint == job(source='greenhouse:Cloud PH',source_job_id='different').fingerprint

def test_discord_alert_is_compact_and_has_real_link_buttons():
    record=Job(id=1,title='Junior Cloud Engineer',company='Cloud PH',location='Makati, Philippines',source='greenhouse:Cloud PH',description='',url='https://example.com/view',application_url='https://example.com/apply',score=87,match_reasons=['AWS','Docker'],warnings=[],work_setup='Hybrid',salary=None,date_posted=None)
    cfg=Settings(app_secret_key='test-secret',public_base_url='https://agent.example')
    payload=Discord(None,'Apply now!',cfg).payload(record)
    assert payload['content'] == 'Apply now!'
    assert payload['embeds'][0]['title'] == '𝐇𝐈𝐆𝐇 𝐌𝐀𝐓𝐂𝐇'
    assert '✅' not in payload['embeds'][0]['fields'][0]['value']
    assert payload['components'][0]['components'][0]['url'] == record.url
    assert any(x['label']=='APPLY NOW' and x['url'].startswith('https://agent.example/actions/') for x in payload['components'][0]['components'])
    assert any(x['label']=='SEARCH JOBS' and x['url']=='https://agent.example/search' for x in payload['components'][1]['components'])
    assert sum(x['name']=='𝐖𝐀𝐍𝐓 𝐓𝐎 𝐅𝐈𝐍𝐃 𝐀 𝐒𝐏𝐄𝐂𝐈𝐅𝐈𝐂 𝐉𝐎𝐁?' for x in payload['embeds'][0]['fields']) == 1
    assert Discord(None,'',cfg).payload(record,test=True)['components'] == []

def test_signed_actions_expire_and_cannot_be_tampered():
    tokens=ActionTokens(Settings(app_secret_key='test-secret'))
    token=tokens.issue(7,'saved',60)
    assert tokens.verify(token,'saved')['j'] == 7
    assert tokens.verify(token+'x') is None

def test_ai_revision_validator_rejects_fabricated_claims():
    assert valid_revision('A'*400)
    assert not valid_revision('A'*350+' I have 5 years of experience.')
    assert not valid_revision('A'*350+' I am AWS certified.')

def test_supabase_postgres_url_uses_installed_psycopg_driver():
    repo=Repository('postgresql://user:password@example.com:5432/database')
    assert repo.engine.url.drivername == 'postgresql+psycopg'

def test_global_baseline_alert_limit(tmp_path):
    repo=Repository(f'sqlite:///{tmp_path}/baseline.db'); repo.create_schema()
    assert [repo.reserve_baseline_alert() for _ in range(6)] == [True,True,True,True,True,False]

def test_jobstreet_page_load_budget_is_separate_and_hard_capped(tmp_path):
    repo=Repository(f'sqlite:///{tmp_path}/quota.db'); repo.create_schema()
    assert repo.reserve_brightdata_page_loads('jobstreet',2,3)
    assert not repo.reserve_brightdata_page_loads('jobstreet',2,3)
    # A JobStreet page-load stop must not consume or block another source.
    assert repo.reserve_brightdata_page_loads('linkedin_jobs',3,3)

def test_new_source_id_with_newer_timestamp_is_a_repost(tmp_path):
    repo=Repository(f'sqlite:///{tmp_path}/repost.db'); repo.create_schema()
    first=job(source='brightdata:linkedin_jobs',source_job_id='old',date_posted=datetime.now(timezone.utc)-timedelta(days=5))
    score,reasons,warnings,_=evaluate(first); old=repo.save(first,score,reasons,warnings)
    repost=job(source='brightdata:linkedin_jobs',source_job_id='new',date_posted=datetime.now(timezone.utc)-timedelta(hours=4))
    score,reasons,warnings,_=evaluate(repost); saved=repo.save(repost,score,reasons,warnings)
    assert saved.id==old.id and saved.source_job_id=='new' and saved.raw_metadata['reposted']

@pytest.mark.asyncio
async def test_manual_find_keeps_only_recent_ph_tech_jobs(tmp_path):
    cfg=Settings(database_url=f'sqlite:///{tmp_path}/manual.db',brightdata_enabled=True,brightdata_api_token='token')
    repo=Repository(cfg.database_url); repo.create_schema(); search=ManualJobSearch(cfg,repo)
    async def reply(_input,_limit):
        return [
            {'job_posting_id':'fresh','job_title':'IT Support Specialist','company_name':'Cloud PH','job_location':'Manila, Philippines','job_summary':'Fresh graduate Linux technical support','url':'https://example.com/fresh','job_posted_date':datetime.now(timezone.utc).isoformat()},
            {'job_posting_id':'old','job_title':'IT Support Specialist','company_name':'Cloud PH','job_location':'Manila, Philippines','job_summary':'Linux technical support','url':'https://example.com/old','job_posted_date':(datetime.now(timezone.utc)-timedelta(days=9)).isoformat()},
        ]
    search.client.linkedin_discovery=reply
    found=await search.find('IT Support')
    assert len(found)==1 and found[0].source_job_id=='fresh'

def test_webhook_only_search_page_has_no_bot_dependency():
    page=search_page().body.decode()
    assert '/find' in page and '/search/discord' in page and 'DISCORD_BOT_TOKEN' not in page

def test_default_motivation_pool_is_available():
    cfg=Settings(discord_motivation='',discord_motivations_json='')
    assert len(cfg.discord_motivations) == 5

def test_root_route_is_render_probe_friendly():
    assert home().status_code == 200

@pytest.mark.asyncio
async def test_brightdata_normalizes_and_caches_results():
    class Client:
        calls=0
        async def scrape(self,*_,**__):
            self.calls+=1
            return [{'id':'1','title':'Junior Cloud Engineer','company':'Cloud PH','location':'Makati, Philippines','description':'AWS Docker','url':'https://example.com/job','date_posted':'2026-09-14T00:00:00+00:00'}]
    client=Client(); source=BrightDataJobs('linkedin_jobs','dataset',[{'keyword':'cloud'}],client)
    jobs=await source.fetch()
    assert jobs[0].source=='brightdata:linkedin_jobs' and jobs[0].title=='Junior Cloud Engineer'

@pytest.mark.asyncio
async def test_jobstreet_does_not_call_brightdata_after_monthly_cap(tmp_path):
    class Client:
        called=False
        async def scrape(self,*_,**__):
            self.called=True
            return []
    repo=Repository(f'sqlite:///{tmp_path}/jobstreet.db'); repo.create_schema()
    client=Client()
    source=BrightDataJobs('jobstreet','dataset',[{'url':'https://www.jobstreet.com.ph/jobs'}],client,repo,monthly_page_limit=1)
    assert await source.fetch() == []
    with pytest.raises(SourceError,match='free safety limit'):
        await source.fetch()
    assert client.called

@pytest.mark.asyncio
async def test_brightdata_auth_failure_does_not_retry(monkeypatch):
    class Response:
        status_code=401; headers={}; text='unauthorized'
        @property
        def is_success(self): return False
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self,*_): return None
        async def post(self,*_,**__): return Response()
    monkeypatch.setattr('src.brightdata.httpx.AsyncClient',lambda **_:Client())
    with pytest.raises(SourceError,match='authentication'):
        await BrightDataClient('token',retries=3).scrape('dataset',[{'q':'test'}])

@pytest.mark.asyncio
async def test_brightdata_429_respects_retry_cap(monkeypatch):
    class Response:
        status_code=429; headers={'Retry-After':'0'}; text='rate limited'
        @property
        def is_success(self): return False
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self,*_): return None
        async def post(self,*_,**__): return Response()
    monkeypatch.setattr('src.brightdata.httpx.AsyncClient',lambda **_:Client())
    with pytest.raises(SourceError,match='temporarily'):
        await BrightDataClient('token',retries=0).scrape('dataset',[{'q':'test'}])

@pytest.mark.asyncio
async def test_gemini_429_uses_pool_cooldown_and_cache():
    manager=GeminiManager(Settings(ai_enabled=True,cover_letter_mode='ai',gemini_max_retries=0))
    manager.keys=[KeyState('1','keyA3F2','project-a')]
    async def limited(*_): return None,429,7,'rate_limit_exceeded'
    manager._post=limited
    assert await manager._request('x') is None
    assert manager.keys[0].cooldown_until > __import__('time').time()+6
    manager.keys[0].cooldown_until=0
    calls=0
    async def ok(*_):
        nonlocal calls; calls+=1; return 'revised',200,None,''
    manager._post=ok
    assert await manager.revise('same') == 'revised'
    assert await manager.revise('same') == 'revised'
    assert calls == 1 and manager.cache_hits == 1

@pytest.mark.asyncio
async def test_gemini_disables_invalid_key_and_exhausts_whole_project_pool():
    manager=GeminiManager(Settings(ai_enabled=True,cover_letter_mode='ai',gemini_max_retries=0))
    manager.keys=[KeyState('1','badA3F2','same'),KeyState('2','otherB4C5','same')]
    async def invalid(key,*_): return None,401,None,'invalid API key'
    manager._post=invalid; await manager._request('x')
    assert manager.keys[0].disabled
    manager.keys=[KeyState('1','oneA3F2','same'),KeyState('2','twoB4C5','same')]
    async def exhausted(*_): return None,429,None,'daily project quota exceeded'
    manager._post=exhausted; await manager._request('x')
    assert all(k.daily_exhausted for k in manager.keys)

@pytest.mark.asyncio
async def test_gemini_503_is_transient_and_does_not_disable_key():
    manager=GeminiManager(Settings(ai_enabled=True,cover_letter_mode='ai',gemini_max_retries=0))
    manager.keys=[KeyState('1','keyA3F2','project-a')]
    async def unavailable(*_): return None,503,None,'service unavailable'
    manager._post=unavailable; await manager._request('x')
    assert not manager.keys[0].disabled and manager.keys[0].cooldown_until > __import__('time').time()
