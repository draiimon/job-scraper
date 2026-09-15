"""Small Discord-flow regression tests that do not need a live Discord API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import discord
import pytest

from src.config import Settings
from src.discord_bot import discord_timestamp, make_after_hours_embed, run_discord_bot
from src.models import Job
from src.services import Repository


class _FakeMessage:
    def __init__(self, embed=None):
        self.embeds = [embed] if embed is not None else []
        self.edits: list[dict] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "embed" in kwargs:
            self.embeds = [kwargs["embed"]]


class _FakeChannel:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs):
        message = _FakeMessage(kwargs.get("embed"))
        self.sent.append({"content": content, "message": message, **kwargs})
        return message


class _FakeClient:
    instance = None

    def __init__(self, *_args, **_kwargs):
        self.closed = False
        self.ready = asyncio.Event()
        self.stop_requested = asyncio.Event()
        _FakeClient.instance = self

    def event(self, handler):
        setattr(self, handler.__name__, handler)
        return handler

    def add_view(self, _view):
        pass

    def get_channel(self, _channel_id):
        return None

    async def fetch_channel(self, _channel_id):
        return None

    def is_closed(self):
        return self.closed

    async def start(self, _token):
        await self.on_ready()
        self.ready.set()
        await self.stop_requested.wait()

    async def close(self):
        self.closed = True
        self.stop_requested.set()


class _Author:
    bot = False


class _TextMessage:
    _next_id = 1

    def __init__(self, content, channel):
        self.id = _TextMessage._next_id
        _TextMessage._next_id += 1
        self.content = content
        self.channel = channel
        self.author = _Author()


def _job() -> Job:
    return Job(
        fingerprint="discord-flow-fixture",
        source="greenhouse:fixture",
        source_job_id="fixture-1",
        title="Junior DevOps Engineer",
        company="Cloud PH",
        location="Manila, Philippines",
        description="AWS Docker Terraform Linux",
        url="https://example.com/jobs/1",
        application_url="https://example.com/jobs/1/apply",
        score=85,
        match_reasons=["AWS", "Docker"],
        warnings=[],
        raw_metadata={},
        status="NEW",
        notification_state="SENT",
        date_posted=datetime.now(timezone.utc),
    )


def test_shared_embed_factory_and_timestamp_are_safe_for_discord():
    embed = make_after_hours_embed("STATUS", "Ready")
    assert embed.title == "STATUS"
    assert embed.description == "Ready"
    assert embed.footer.text
    assert discord_timestamp("not-a-date") == "—"
    rendered = discord_timestamp("2026-09-15T10:30:00+00:00")
    assert rendered.startswith("<t:") and ":R>" in rendered


@pytest.mark.asyncio
async def test_viewall_alias_is_stored_only_and_clamps_an_out_of_range_page(monkeypatch, tmp_path):
    """`v!view all` must not trigger a scan or render a nonsense `Page 99 / 1`."""

    monkeypatch.setattr(discord, "Client", _FakeClient)
    repo = Repository(f"sqlite:///{tmp_path / 'discord-flow.db'}")
    repo.create_schema()
    with repo.sessions() as session:
        session.add(_job())
        session.commit()

    scans = 0

    async def manual_scan():
        nonlocal scans
        scans += 1
        return []

    def snapshot():
        return {"status": "running", "phase": "idle", "sources_working": 1}

    cfg = Settings(database_url=f"sqlite:///{tmp_path / 'discord-flow.db'}", discord_bot_token="test-token")
    bot_task = asyncio.create_task(run_discord_bot(cfg, repo, None, snapshot, manual_scan))
    await asyncio.sleep(0)
    client = _FakeClient.instance
    await asyncio.wait_for(client.ready.wait(), timeout=2)
    channel = _FakeChannel()

    try:
        # The spaced alias and an absurd page request both use the stored job
        # board. Neither command is allowed to launch a fresh source scan.
        await client.on_message(_TextMessage("v!view all", channel))
        await client.on_message(_TextMessage("v!viewall 99", channel))

        assert scans == 0
        assert len(channel.sent) == 2
        for sent in channel.sent:
            final_embed = sent["message"].edits[-1]["embed"]
            navigation = next(field.value for field in final_embed.fields if "Page " in field.value)
            assert "Page 1 of 1" in navigation
            assert "Junior DevOps Engineer" in final_embed.description
    finally:
        client.stop_requested.set()
        await asyncio.wait_for(bot_task, timeout=2)
