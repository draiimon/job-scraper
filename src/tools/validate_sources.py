from __future__ import annotations
import asyncio
from ..config import settings
from ..sources import configured_sources, SourceError

async def main():
    sources=configured_sources(settings().source_targets)
    if not sources: print('No configured public ATS sources.'); return 1
    failed=0
    for source in sources:
        try:
            jobs=await source.fetch(); status='PASS' if jobs else 'EMPTY_VALID'
            print(f'{source.name} | {status} | {len(jobs)} jobs')
        except SourceError as exc:
            failed+=1; print(f'{source.name} | UNREACHABLE | 0 jobs | {exc}')
        except Exception as exc:
            failed+=1; print(f'{source.name} | INVALID | 0 jobs | {exc}')
    return failed
if __name__=='__main__': raise SystemExit(asyncio.run(main()))
