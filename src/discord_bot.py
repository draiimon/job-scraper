from __future__ import annotations
import logging
from .security import ActionTokens

log=logging.getLogger(__name__)

async def run_discord_bot(cfg, repo, manual_search, scheduler_snapshot, manual_scan):
    """Optional Discord bot. Webhook alerts remain independent and unchanged."""
    if not cfg.discord_bot_token:
        return
    try:
        import discord
    except ImportError:
        log.error('discord_bot_dependency_missing')
        return
    intents=discord.Intents.default()
    intents.message_content=True
    bot=discord.Client(intents=intents)

    def action_url(job, action):
        if not cfg.public_base_url: return None
        token=ActionTokens(cfg).issue(job.id,action)
        return f'{cfg.public_base_url.rstrip("/")}/actions/{token}' if token else None
    def card(job):
        embed=discord.Embed(title='𝐇𝐈𝐆𝐇 𝐌𝐀𝐓𝐂𝐇' if job.score>=85 else '𝐄𝐍𝐓𝐑𝐘-𝐋𝐄𝐕𝐄𝐋 𝐓𝐄𝐂𝐇',description=f'**{job.score}% MATCH**\n\n**{job.title}**\n{job.company}\n{job.location}',colour=0xF59E0B,url=job.url)
        if job.match_reasons: embed.add_field(name='𝐖𝐇𝐘 𝐈𝐓 𝐅𝐈𝐓𝐒',value='\n'.join(job.match_reasons[:5]),inline=False)
        embed.set_footer(text='After Hours Job Hunter • Made by masoncalix')
        view=discord.ui.View(timeout=900)
        view.add_item(discord.ui.Button(label='VIEW JOB',style=discord.ButtonStyle.link,url=job.url))
        for label,action in (('APPLY NOW','review'),('SAVE','saved'),('SKIP','ignored')):
            if url:=action_url(job,action): view.add_item(discord.ui.Button(label=label,style=discord.ButtonStyle.link,url=url))
        return embed,view
    async def send_jobs(channel,jobs):
        if not jobs:
            await channel.send('No recent qualifying jobs found.'); return
        for job in jobs[:3]:
            embed,view=card(job); await channel.send(embed=embed,view=view)
    async def help_message(channel):
        embed=discord.Embed(title='𝐀𝐅𝐓𝐄𝐑 𝐇𝐎𝐔𝐑𝐒 𝐉𝐎𝐁 𝐇𝐔𝐍𝐓𝐄𝐑 — 𝐇𝐄𝐋𝐏',description='Use the bot to search recent PH tech jobs without waiting for the next scheduled scan.',colour=0xF59E0B)
        embed.add_field(name='𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒',value='`v!search Junior DevOps` — latest matching jobs\n`v!latest` — newest stored qualifying jobs\n`v!status` — monitor health\n`v!scan` — secure one-time scan link\n`v!help` — this guide',inline=False)
        embed.add_field(name='𝐀𝐔𝐓𝐎𝐌𝐀𝐓𝐈𝐎𝐍',value='The monitor scans every 15 minutes. Apply opens review; Save keeps a role; Skip stops future alerts.',inline=False)
        embed.set_footer(text='After Hours Job Hunter • Made by masoncalix')
        await channel.send(embed=embed)
    @bot.event
    async def on_ready():
        log.info('discord_bot_ready')
    @bot.event
    async def on_message(message):
        if message.author.bot: return
        content=message.content.strip()
        if not content.lower().startswith('v!'): return
        command, _, argument=content[2:].strip().partition(' '); command=command.lower(); argument=argument.strip()
        if command=='help': await help_message(message.channel); return
        if command=='search':
            if not argument:
                await message.channel.send('Usage: `v!search Junior DevOps`'); return
            try: jobs=await manual_search.find(argument)
            except Exception:
                await message.channel.send('Search is temporarily unavailable. Existing alerts continue normally.'); return
            await send_jobs(message.channel,jobs); return
        if command=='latest':
            from sqlalchemy import select
            from .models import Job, JobStatus
            with repo.sessions() as s: jobs=s.scalars(select(Job).where(Job.status!=JobStatus.EXPIRED.value,Job.score>=cfg.min_notify_score).order_by(Job.date_posted.desc()).limit(3)).all()
            await send_jobs(message.channel,jobs); return
        if command=='status':
            state=scheduler_snapshot(); await message.channel.send(f"𝐉𝐎𝐁 𝐇𝐔𝐍𝐓𝐄𝐑 𝐒𝐓𝐀𝐓𝐔𝐒\nScheduler: {state.get('status','starting').upper()}\nSources: {state.get('sources_working',0)} active\nLinkedIn: {state.get('linkedin_status')}\nJobStreet: {state.get('jobstreet_status')}"); return
        if command=='scan':
            token=ActionTokens(cfg).issue_control('scan')
            if token and cfg.public_base_url: await message.channel.send(f'Start one protected scan: {cfg.public_base_url.rstrip("/")}/control/scan/{token}')
            else: await message.channel.send('Secure Scan Now is not configured.');
            return
        await message.channel.send('Unknown command. Use `v!help`.')
    try:
        await bot.start(cfg.discord_bot_token)
    finally:
        if not bot.is_closed(): await bot.close()
