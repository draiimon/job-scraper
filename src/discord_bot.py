from __future__ import annotations
import asyncio, logging, time
from datetime import datetime
from zoneinfo import ZoneInfo
from .security import ActionTokens

log=logging.getLogger(__name__)

async def run_discord_bot(cfg, repo, manual_search, scheduler_snapshot, manual_scan):
    if not cfg.discord_bot_token:
        return
    try:
        import discord
    except ImportError:
        log.error('discord_bot_dependency_missing'); return
    intents=discord.Intents.default(); intents.message_content=True
    bot=discord.Client(intents=intents); panel_channel=None; panel_view=None; panel_task=None; registered=False; search_cooldowns={}

    def stamp(value):
        if not value: return '—'
        try: return datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Manila')).strftime('%I:%M %p')
        except ValueError: return '—'
    def panel_embed():
        state=scheduler_snapshot(); scanning=state.get('phase')=='scanning'
        embed=discord.Embed(title='𝐀𝐅𝐓𝐄𝐑 𝐇𝐎𝐔𝐑𝐒 𝐉𝐎𝐁 𝐇𝐔𝐍𝐓𝐄𝐑',description='Your automated Philippine tech-job monitor is online.',colour=0xF59E0B)
        embed.add_field(name='𝐒𝐘𝐒𝐓𝐄𝐌 𝐒𝐓𝐀𝐓𝐔𝐒',value=f"Service\nONLINE\n\nScheduler\n{state.get('status','starting').upper()}\n\nLast scan\n{stamp(state.get('last_poll_at'))}\n\nNext scan\n{stamp(state.get('next_poll_at'))}",inline=True)
        embed.add_field(name='𝐂𝐎𝐍𝐍𝐄𝐂𝐓𝐈𝐎𝐍𝐒',value=f"Sources\n{state.get('sources_working',0)} active\n\nLinkedIn\n{state.get('linkedin_status')}\n\nJobStreet\n{state.get('jobstreet_status')}\n\nDatabase\nCONNECTED\n\nDiscord\nCONNECTED",inline=True)
        embed.add_field(name='𝐒𝐂𝐀𝐍𝐍𝐈𝐍𝐆 𝐍𝐎𝐖' if scanning else '𝐒𝐂𝐀𝐍 𝐂𝐎𝐌𝐏𝐋𝐄𝐓𝐄',value='Checking recent active jobs…' if scanning else f"Jobs checked: {state.get('jobs_checked',0)}\nNew qualifying jobs: {state.get('new_recent_jobs',0)}\nAlerts sent: {state.get('alerts_sent',0)}",inline=False)
        embed.add_field(name='𝐇𝐎𝐖 𝐓𝐎 𝐔𝐒𝐄',value='Use **SEARCH JOBS** or type `v!search Junior DevOps`. Everything happens inside Discord.',inline=False)
        embed.set_footer(text='After Hours Job Hunter • Made by masoncalix')
        return embed
    class JobView(discord.ui.View):
        def __init__(self, job):
            super().__init__(timeout=900); self.job_id=job.id
            self.add_item(discord.ui.Button(label='VIEW JOB',style=discord.ButtonStyle.link,url=job.url))
        @discord.ui.button(label='APPLY NOW',style=discord.ButtonStyle.primary)
        async def apply(self,interaction,button):
            from .models import Job
            with repo.sessions() as s: job=s.get(Job,self.job_id)
            if not job: await interaction.response.send_message('This job is no longer available.',ephemeral=True); return
            embed=discord.Embed(title='𝐀𝐏𝐏𝐋𝐈𝐂𝐀𝐓𝐈𝐎𝐍 𝐑𝐄𝐕𝐈𝐄𝐖',description=f'**{job.title}**\n{job.company}\n\nUse VIEW JOB to open the employer application portal. No application is sent automatically.',colour=0xF59E0B)
            await interaction.response.send_message(embed=embed,ephemeral=True)
        @discord.ui.button(label='SAVE',style=discord.ButtonStyle.secondary)
        async def save(self,interaction,button):
            from .models import Job
            with repo.sessions() as s:
                job=s.get(Job,self.job_id)
                if not job: await interaction.response.send_message('This job is no longer available.',ephemeral=True); return
                job.status='SAVED'; s.commit()
            embed=interaction.message.embeds[0]; embed.add_field(name='𝐒𝐓𝐀𝐓𝐔𝐒',value='Saved',inline=False)
            await interaction.message.edit(embed=embed,view=self)
            await interaction.response.send_message('Saved for later.',ephemeral=True)
        @discord.ui.button(label='SKIP',style=discord.ButtonStyle.secondary)
        async def skip(self,interaction,button):
            from .models import Job
            with repo.sessions() as s:
                job=s.get(Job,self.job_id)
                if not job: await interaction.response.send_message('This job is no longer available.',ephemeral=True); return
                job.status='IGNORED'; s.commit()
            embed=interaction.message.embeds[0]; embed.add_field(name='𝐒𝐓𝐀𝐓𝐔𝐒',value='Skipped',inline=False)
            for child in self.children:
                if getattr(child,'label',None) in ('APPLY NOW','SAVE','SKIP'): child.disabled=True
            await interaction.message.edit(embed=embed,view=self)
            await interaction.response.send_message('Skipped. Future alerts for this vacancy are stopped.',ephemeral=True)
    def card(job):
        embed=discord.Embed(title='𝐇𝐈𝐆𝐇 𝐌𝐀𝐓𝐂𝐇' if job.score>=85 else '𝐄𝐍𝐓𝐑𝐘-𝐋𝐄𝐕𝐄𝐋 𝐓𝐄𝐂𝐇',description=f'**{job.score}% MATCH**\n\n**{job.title}**\n{job.company}\n{job.location}',colour=0xF59E0B,url=job.url)
        if job.match_reasons: embed.add_field(name='𝐖𝐇𝐘 𝐈𝐓 𝐅𝐈𝐓𝐒',value='\n'.join(job.match_reasons[:5]),inline=False)
        embed.set_footer(text='After Hours Job Hunter • Made by masoncalix')
        return embed,JobView(job)
    async def send_jobs(interaction,jobs):
        if not jobs: await interaction.followup.send('No recent qualifying jobs found.',ephemeral=True); return
        for job in jobs[:3]:
            embed,view=card(job); await interaction.followup.send(embed=embed,view=view,ephemeral=True)
    async def help_response(interaction):
        embed=discord.Embed(title='𝐇𝐎𝐖 𝐓𝐎 𝐔𝐒𝐄',description='`v!search <role>` — latest roles\n`v!latest` — newest stored jobs\n`v!status` — monitor health\n`v!scan` — one protected scan\n`v!help` — this guide',colour=0xF59E0B); embed.set_footer(text='After Hours Job Hunter • Made by masoncalix')
        await interaction.response.send_message(embed=embed,ephemeral=True)
    class SearchModal(discord.ui.Modal,title='Find recent jobs'):
        role=discord.ui.TextInput(label='Job role / keyword',placeholder='Junior DevOps',max_length=100)
        location=discord.ui.TextInput(label='Location',default='Philippines',required=False,max_length=100)
        freshness=discord.ui.TextInput(label='Freshness',default='Past 24 hours',required=False,max_length=30)
        work_setup=discord.ui.TextInput(label='Work setup',placeholder='Remote / Hybrid / On-site',required=False,max_length=30)
        minimum_score=discord.ui.TextInput(label='Minimum match score',default='0',required=False,max_length=3)
        async def on_submit(self,interaction):
            if search_cooldowns.get(interaction.user.id,0)>time.time(): await interaction.response.send_message('Please wait before searching again.',ephemeral=True); return
            search_cooldowns[interaction.user.id]=time.time()+45
            await interaction.response.defer(ephemeral=True,thinking=True)
            try: jobs=await manual_search.find(str(self.role),str(self.location or 'Philippines'),str(self.freshness or 'Past 24 hours'),'',5,str(self.work_setup),max(0,min(100,int(str(self.minimum_score or '0')))))
            except Exception: await interaction.followup.send('Search is temporarily unavailable.',ephemeral=True); return
            await send_jobs(interaction,jobs)
    class ControlView(discord.ui.View):
        def __init__(self): super().__init__(timeout=None)
        @discord.ui.button(label='SCAN NOW',style=discord.ButtonStyle.primary,custom_id='jobhunter:scan')
        async def scan(self,interaction,button):
            await interaction.response.defer(ephemeral=True,thinking=True)
            try: await manual_scan(); await refresh_panel(); await interaction.followup.send('Scan complete.',ephemeral=True)
            except Exception as exc: await interaction.followup.send(str(getattr(exc,'detail','Scan unavailable.')),ephemeral=True)
        @discord.ui.button(label='SEARCH JOBS',style=discord.ButtonStyle.secondary,custom_id='jobhunter:search')
        async def search(self,interaction,button): await interaction.response.send_modal(SearchModal())
        @discord.ui.button(label='VIEW STATUS',style=discord.ButtonStyle.secondary,custom_id='jobhunter:status')
        async def status(self,interaction,button): await interaction.response.send_message(embed=panel_embed(),ephemeral=True)
        @discord.ui.button(label='VIEW LATEST JOBS',style=discord.ButtonStyle.secondary,custom_id='jobhunter:latest')
        async def latest(self,interaction,button):
            from sqlalchemy import select
            from .models import Job, JobStatus
            await interaction.response.defer(ephemeral=True,thinking=True)
            with repo.sessions() as s: jobs=s.scalars(select(Job).where(Job.status!=JobStatus.EXPIRED.value,Job.score>=cfg.min_notify_score).order_by(Job.date_posted.desc()).limit(3)).all()
            await send_jobs(interaction,jobs)
        @discord.ui.button(label='HELP',style=discord.ButtonStyle.secondary,custom_id='jobhunter:help')
        async def help(self,interaction,button): await help_response(interaction)
    async def resolve_channel():
        nonlocal panel_channel
        if panel_channel: return panel_channel
        if not cfg.discord_webhook_url: return None
        try:
            hook=discord.Webhook.from_url(cfg.discord_webhook_url,client=bot); fetched=await hook.fetch()
            panel_channel=bot.get_channel(fetched.channel_id) or await bot.fetch_channel(fetched.channel_id)
        except Exception as exc: log.warning('discord_control_channel_unavailable',extra={'error':str(exc)})
        return panel_channel
    async def refresh_panel():
        channel=await resolve_channel()
        if not channel: return
        message_id=repo.state('discord_bot_control_panel_message_id')
        if message_id:
            try:
                message=await channel.fetch_message(int(message_id)); await message.edit(embed=panel_embed(),view=panel_view); return
            except Exception: repo.set_state('discord_bot_control_panel_message_id',None)
        message=await channel.send(embed=panel_embed(),view=panel_view); repo.set_state('discord_bot_control_panel_message_id',str(message.id))
    async def deliver_pending_alerts():
        from sqlalchemy import select
        from .models import Job, JobStatus
        channel=await resolve_channel()
        if not channel: return
        with repo.sessions() as s: ids=s.scalars(select(Job.id).where(Job.notification_state=='BOT_PENDING').limit(3)).all()
        for job_id in ids:
            with repo.sessions() as s: job=s.get(Job,job_id)
            if not job: continue
            try:
                embed,view=card(job); await channel.send(embed=embed,view=view)
                with repo.sessions() as s:
                    stored=s.get(Job,job_id); stored.notification_state='SENT'; stored.status=JobStatus.NOTIFIED.value; s.commit()
            except Exception as exc: log.warning('discord_bot_alert_failed',extra={'job_id':job_id,'error':str(exc)})
    async def panel_watcher():
        last=None
        while not bot.is_closed():
            state=scheduler_snapshot(); marker=(state.get('phase'),state.get('last_poll_at'),state.get('next_poll_at'))
            if marker!=last: await refresh_panel(); last=marker
            await deliver_pending_alerts()
            await asyncio.sleep(10)
    @bot.event
    async def on_ready():
        nonlocal panel_view,panel_task,registered
        if not registered:
            panel_view=ControlView(); bot.add_view(panel_view); registered=True
        await refresh_panel()
        if not panel_task: panel_task=asyncio.create_task(panel_watcher())
        repo.set_state('discord_bot_health',{'healthy':True})
        log.info('discord_bot_ready')
    @bot.event
    async def on_message(message):
        if message.author.bot or not message.content.lower().startswith('v!'): return
        command,_,argument=message.content[2:].strip().partition(' '); command=command.lower(); argument=argument.strip()
        if command=='help':
            class Fake: pass
            await message.channel.send(embed=discord.Embed(title='𝐇𝐎𝐖 𝐓𝐎 𝐔𝐒𝐄',description='`v!search <role>`\n`v!latest`\n`v!status`\n`v!scan`\n`v!help`',colour=0xF59E0B)); return
        if command=='search':
            if not argument: await message.channel.send('Usage: `v!search Junior DevOps`'); return
            if search_cooldowns.get(message.author.id,0)>time.time(): await message.channel.send('Please wait before searching again.'); return
            search_cooldowns[message.author.id]=time.time()+45
            parts=[x.strip() for x in argument.split('|')]; role=parts[0]; location=parts[1] if len(parts)>1 and parts[1] else 'Philippines'; freshness='Past 24 hours' if len(parts)<3 or not parts[2] else ('Past 24 hours' if parts[2].lower() in ('24h','24 hours') else f'Past {parts[2]}')
            try: jobs=await manual_search.find(role,location,freshness,'',5)
            except Exception: await message.channel.send('Search is temporarily unavailable.'); return
            for job in jobs[:3]:
                embed,view=card(job); await message.channel.send(embed=embed,view=view)
            if not jobs: await message.channel.send('No recent qualifying jobs found.')
        elif command=='latest':
            from sqlalchemy import select
            from .models import Job, JobStatus
            with repo.sessions() as s: jobs=s.scalars(select(Job).where(Job.status!=JobStatus.EXPIRED.value,Job.score>=cfg.min_notify_score).order_by(Job.date_posted.desc()).limit(3)).all()
            for job in jobs:
                embed,view=card(job); await message.channel.send(embed=embed,view=view)
        elif command=='status': await message.channel.send(embed=panel_embed())
        elif command=='scan':
            try: await manual_scan(); await refresh_panel(); await message.channel.send('Scan complete.')
            except Exception as exc: await message.channel.send(str(getattr(exc,'detail','Scan unavailable.')))
        else: await message.channel.send('Use `v!help`.')
    try: await bot.start(cfg.discord_bot_token)
    finally:
        repo.set_state('discord_bot_health',{'healthy':False})
        if panel_task: panel_task.cancel()
        if not bot.is_closed(): await bot.close()
