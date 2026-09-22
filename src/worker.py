"""Dedicated production discovery worker entrypoint.

The web service can run with ``POLLING_ENABLED=false`` while this process owns
the durable scheduler lease. Local Docker can continue using the single
lifespan-managed process for convenience.
"""

from __future__ import annotations

import asyncio

from .config import settings
from .main import cfg, discord_worker, repo, worker


async def serve() -> None:
    tasks = [asyncio.create_task(worker(), name="discovery-worker")]
    if cfg.discord_bot_token:
        tasks.append(asyncio.create_task(discord_worker(), name="discord-gateway"))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    runtime = settings()
    repo.initialize_runtime_config(runtime)
    repo.expire_stale_jobs()
    asyncio.run(serve())


if __name__ == "__main__":
    main()
