from datetime import datetime, timedelta, timezone
import pytest
from src.config import Settings
from src.jobs import NormalizedJob, evaluate, is_ph_location
from src.services import Pipeline, Repository
from src.applications import cover_letter, eligible_for_email
from src.email_alerts import alert_to_job
from src.services import Discord
from src.models import Job
from src.ai import GeminiManager, KeyState
from src.security import ActionTokens
from src.applications import valid_revision

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

def test_broad_technology_role_is_eligible_but_senior_is_not():
    support=job(title='IT Support Specialist',description='Fresh graduate opportunity supporting Windows, Linux, networks, and technical users.')
    score,_,_,valid=evaluate(support)
    assert valid and score >= 60
    senior=evaluate(job(title='Senior Software Engineer',description='Python Docker requires 5+ years'))
    assert senior[0] == 0

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
    assert payload['embeds'][0]['title'] == '🔔 High match · 87%'
    assert '✅' not in payload['embeds'][0]['fields'][0]['value']
    assert payload['components'][0]['components'][0]['url'] == record.url
    assert any(x['label']=='APPLY NOW' and x['url'].startswith('https://agent.example/actions/') for x in payload['components'][0]['components'])
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
