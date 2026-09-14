import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest

from src.config import Settings
from src.discord_bot import run_discord_bot
from src.models import Job
from src.services import Repository


class FakeResponse:
    def __init__(self, interaction):
        self.interaction = interaction

    async def _ack(self, kind, **payload):
        self.interaction.ack_kind = kind
        self.interaction.payload = payload
        self.interaction.ack_at = time.monotonic()
        self.interaction.acknowledged.set()

    async def send_message(self, content=None, **kwargs):
        await self._ack("message", content=content, **kwargs)

    async def defer(self, **kwargs):
        await self._ack("defer", **kwargs)

    async def send_modal(self, modal):
        self.interaction.modal = modal
        await self._ack("modal", modal=modal)


class FakeFollowup:
    def __init__(self, interaction):
        self.interaction = interaction
        self.sent = []

    async def send(self, content=None, **kwargs):
        message = FakeMessage(kwargs.get("embed"))
        self.sent.append({"content": content, "message": message, **kwargs})
        return message


class FakeMessage:
    def __init__(self, embed=None):
        self.embeds = [embed] if embed else []
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "embed" in kwargs:
            self.embeds = [kwargs["embed"]]


class FakeInteraction:
    def __init__(self, message=None):
        self.user = SimpleNamespace(id=1)
        self.message = message or FakeMessage(discord.Embed(title="fixture"))
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)
        self.acknowledged = asyncio.Event()
        self.ack_at = None
        self.ack_kind = None
        self.payload = {}
        self.modal = None


class FakeDiscordClient:
    instance = None

    def __init__(self, *args, **kwargs):
        self.closed = False
        self.view = None
        self.ready = asyncio.Event()
        self.stop_requested = asyncio.Event()
        FakeDiscordClient.instance = self

    def event(self, handler):
        setattr(self, handler.__name__, handler)
        return handler

    def add_view(self, view):
        self.view = view

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


async def assert_acknowledged(callback, interaction, timeout=2.95):
    started = time.monotonic()
    task = asyncio.create_task(callback)
    await asyncio.wait_for(interaction.acknowledged.wait(), timeout=timeout)
    assert interaction.ack_at - started < 3
    return task


def button(view, label):
    return next(child for child in view.children if child.label == label)


@pytest.mark.asyncio
async def test_discord_controls_ack_during_slow_scan(monkeypatch, tmp_path):
    monkeypatch.setattr(discord, "Client", FakeDiscordClient)

    repo = Repository(f"sqlite:///{tmp_path / 'discord.db'}")
    repo.create_schema()
    with repo.sessions() as session:
        session.add(
            Job(
                fingerprint="discord-fixture",
                source="fixture",
                source_job_id="fixture-1",
                title="Junior DevOps Engineer",
                company="Cloud PH",
                location="Manila, Philippines",
                work_setup="Hybrid",
                description="AWS Docker Terraform Linux",
                url="https://example.com/job",
                application_url="https://example.com/apply",
                score=90,
                match_reasons=["AWS", "Docker"],
                warnings=[],
                raw_metadata={},
                status="NEW",
                notification_state="SENT",
                    date_posted=datetime.now(timezone.utc),
            )
        )
        session.commit()

    scan_finished = asyncio.Event()

    async def slow_scan():
        await asyncio.sleep(3.05)
        scan_finished.set()
        return []

    class SlowSearch:
        async def find(self, *args):
            await asyncio.sleep(3.05)
            return []

    def slow_snapshot():
        time.sleep(3.05)
        return {
            "status": "running",
            "phase": "scanning",
            "sources_working": 1,
            "next_poll_at": None,
        }

    cfg = Settings(
        database_url=f"sqlite:///{tmp_path / 'discord.db'}",
        discord_bot_token="test-token",
        application_dry_run=True,
        app_secret_key="test-secret",
        public_base_url="https://agent.example",
    )
    bot_task = asyncio.create_task(
        run_discord_bot(cfg, repo, SlowSearch(), slow_snapshot, slow_scan)
    )
    await asyncio.sleep(0)
    client = FakeDiscordClient.instance
    await asyncio.wait_for(client.ready.wait(), timeout=2)
    view = client.view

    try:
        scan_interaction = FakeInteraction()
        scan_task = await assert_acknowledged(
            button(view, "SCAN NOW").callback(scan_interaction), scan_interaction
        )
        await scan_task
        await asyncio.wait_for(scan_finished.wait(), timeout=4)

        search_interaction = FakeInteraction()
        search_task = await assert_acknowledged(
            button(view, "SEARCH JOBS").callback(search_interaction),
            search_interaction,
        )
        await search_task
        assert search_interaction.modal is not None

        # Discord populates TextInput values before invoking on_submit.
        search_interaction.modal.role._value = "DevOps"
        search_interaction.modal.location._value = "Philippines"
        modal_interaction = FakeInteraction()
        modal_task = await assert_acknowledged(
            search_interaction.modal.on_submit(modal_interaction), modal_interaction
        )
        await modal_task

        status_interaction = FakeInteraction()
        status_task = await assert_acknowledged(
            button(view, "VIEW STATUS").callback(status_interaction),
            status_interaction,
        )
        await status_task

        view_all_interaction = FakeInteraction()
        view_all_task = await assert_acknowledged(
            button(view, "VIEW ALL JOBS").callback(view_all_interaction),
            view_all_interaction,
        )
        await view_all_task
        assert view_all_interaction.followup.sent
        assert view_all_interaction.followup.sent[0]["message"].embeds[0].title == "𝐉𝐎𝐁 𝐁𝐎𝐀𝐑𝐃"
        view_all_edit = view_all_interaction.followup.sent[0]["message"].edits[-1]
        assert view_all_edit["embed"].title == "𝐉𝐎𝐁 𝐁𝐎𝐀𝐑𝐃"
        view_all_view = view_all_edit["view"]
        view_all_apply = next(
            child for child in view_all_view.children
            if child.label.startswith("APPLY NOW")
        )
        view_all_apply_interaction = FakeInteraction()
        view_all_apply_task = await assert_acknowledged(
            view_all_apply.callback(view_all_apply_interaction),
            view_all_apply_interaction,
        )
        await view_all_apply_task
        assert view_all_apply_interaction.followup.sent
        assert view_all_apply_interaction.followup.sent[0]["message"].embeds[0].title == "𝐀𝐏𝐏𝐋𝐈𝐂𝐀𝐓𝐈𝐎𝐍 𝐑𝐄𝐕𝐈𝐄𝐖"
        assert "Junior DevOps Engineer" in view_all_apply_interaction.followup.sent[0]["message"].embeds[0].description

        class FakeTextChannel:
            def __init__(self):
                self.sent = []

            async def send(self, content=None, **kwargs):
                message = FakeMessage(kwargs.get("embed"))
                self.sent.append({"content": content, "message": message, **kwargs})
                return message

        class FakeAuthor:
            bot = False

        class FakeTextMessage:
            _next_id = 100

            def __init__(self, content, channel):
                self.id = FakeTextMessage._next_id
                FakeTextMessage._next_id += 1
                self.content = content
                self.author = FakeAuthor()
                self.channel = channel

        text_channel = FakeTextChannel()
        await client.on_message(FakeTextMessage("v!viewall", text_channel))
        await client.on_message(FakeTextMessage("v!view all", text_channel))
        assert len(text_channel.sent) == 2
        assert all(item["message"].embeds[0].title == "𝐉𝐎𝐁 𝐁𝐎𝐀𝐑𝐃" for item in text_channel.sent)

        original_sessions = repo.sessions

        def slow_sessions():
            time.sleep(3.05)
            return original_sessions()

        monkeypatch.setattr(repo, "sessions", slow_sessions)
        latest_interaction = FakeInteraction()
        latest_task = await assert_acknowledged(
            button(view, "VIEW LATEST JOBS").callback(latest_interaction),
            latest_interaction,
        )
        await latest_task
        job_view = latest_interaction.followup.sent[0]["view"]
        monkeypatch.setattr(repo, "sessions", original_sessions)

        help_interaction = FakeInteraction()
        help_task = await assert_acknowledged(
            button(view, "HELP").callback(help_interaction), help_interaction
        )
        await help_task

        # The card controls all acknowledge before touching the database or AI.
        job_message = FakeMessage(discord.Embed(title="fixture"))
        apply_interaction = FakeInteraction(job_message)
        apply_task = await assert_acknowledged(
            button(job_view, "APPLY NOW").callback(apply_interaction),
            apply_interaction,
        )
        await apply_task
        review_view = apply_interaction.followup.sent[0]["view"]

        cover_interaction = FakeInteraction()
        cover_task = await assert_acknowledged(
            button(review_view, "VIEW COVER LETTER").callback(cover_interaction),
            cover_interaction,
        )
        await cover_task

        save_interaction = FakeInteraction(job_message)
        save_task = await assert_acknowledged(
            button(job_view, "SAVE").callback(save_interaction), save_interaction
        )
        await save_task

        skip_interaction = FakeInteraction(job_message)
        skip_task = await assert_acknowledged(
            button(job_view, "SKIP").callback(skip_interaction), skip_interaction
        )
        await skip_task
    finally:
        client.stop_requested.set()
        await asyncio.wait_for(bot_task, timeout=2)