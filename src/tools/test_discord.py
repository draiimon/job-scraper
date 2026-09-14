from __future__ import annotations
import asyncio
import httpx
from ..config import settings
from ..models import Job
from ..services import Discord
async def main():
    url=settings().discord_webhook_url
    if not url: print('DISCORD_NOT_CONFIGURED'); return 2
    try:
        job=Job(id=0,title='Associate Cloud Engineer',company='Example Technologies',location='Makati · Hybrid',source='Greenhouse Test Fixture',description='',url='https://example.com/jobs/associate-cloud-engineer',application_url=None,score=82,match_reasons=['AWS','Docker','Linux','Entry-level compatible'],warnings=['Terraform preferred'],work_setup='Hybrid',salary=None,date_posted=None,status='NEW')
        await Discord(url,settings().discord_motivation,settings()).send_payload(Discord(url,settings().discord_motivation,settings()).payload(job,test=True))
        print('DISCORD_TEST_PASS'); return 0
    except httpx.HTTPError:
        print('DISCORD_TEST_FAIL'); return 1
if __name__=='__main__': raise SystemExit(asyncio.run(main()))
