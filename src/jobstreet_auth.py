from __future__ import annotations

import asyncio

from .config import settings
from .jobstreet import authenticate_jobstreet


async def main() -> None:
    await authenticate_jobstreet(settings())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("JobStreet authentication cancelled.")