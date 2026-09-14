from __future__ import annotations
import asyncio, io, logging, random, time
from datetime import datetime
from pathlib import Path
from .security import ActionTokens

log=logging.getLogger(__name__)

def discord_timestamp(value):
    if not value: return '—'
    try:
        unix=int(datetime.fromisoformat(value).timestamp())
        return f'<t:{unix}:t> • <t:{unix}:R>'
    except (TypeError, ValueError):
        return '—'

async def run_discord_bot(cfg, repo, manual_search, scheduler_snapshot, manual_scan):
    if not cfg.discord_bot_token:
        return
    try:
        import discord
    except ImportError:
        log.error('discord_bot_dependency_missing'); return
    intents=discord.Intents.default(); intents.message_content=True
    bot=discord.Client(intents=intents); panel_channel=None; panel_view=None; panel_task=None; scan_task=None; registered=False; search_cooldowns={}

    async def prepare_letter(job_id, use_ai=True, regenerate=False):
        from .applications import generated_letter
        from .models import Job
        def load_job():
            with repo.sessions() as s: return s.get(Job,job_id)
        job=await asyncio.to_thread(load_job)
        if not job: return None,None,None
        resume_text=await asyncio.to_thread(repo.resume_text)
        letter,mode=await generated_letter(job,use_ai,regenerate,resume_text)
        def store_letter():
            with repo.sessions() as s:
                stored=s.get(Job,job_id)
                if not stored: return None
                stored.raw_metadata={**(stored.raw_metadata or {}),'cover_letter':letter,'cover_letter_mode':mode}
                s.commit()
                return stored
        job=await asyncio.to_thread(store_letter)
        return job,letter,mode

    def set_bot_footer(embed):
        footer={'text':'After Hours Job Hunter • Made by masoncalix'}
        avatar=getattr(getattr(bot,'user',None),'display_avatar',None)
        if avatar: footer['icon_url']=str(avatar.url)
        embed.set_footer(**footer)
        return embed
    def styled_embed(title,description='',url=None,colour=0xF59E0B):
        kwargs={'title':title,'description':description,'colour':colour}
        if url: kwargs['url']=url
        return set_bot_footer(discord.Embed(**kwargs))
    async def panel_embed():
        state=await asyncio.to_thread(scheduler_snapshot); scanning=state.get('phase')=='scanning'
        embed=styled_embed('𝐀𝐅𝐓𝐄𝐑 𝐇𝐎𝐔𝐑𝐒 𝐉𝐎𝐁 𝐇𝐔𝐍𝐓𝐄𝐑','Your automated Philippine tech-job monitor is online.')
        embed.add_field(name='𝐒𝐘𝐒𝐓𝐄𝐌 𝐒𝐓𝐀𝐓𝐔𝐒',value=f"Service\nONLINE\n\nScheduler\n{state.get('status','starting').upper()}\n\nLast scan\n{discord_timestamp(state.get('last_poll_at'))}\n\nNext scan\n{discord_timestamp(state.get('next_poll_at'))}",inline=True)
        embed.add_field(name='𝐂𝐎𝐍𝐍𝐄𝐂𝐓𝐈𝐎𝐍𝐒',value=f"Sources\n{state.get('sources_working',0)} active\n\nLinkedIn\n{state.get('linkedin_status')}\n\nJobStreet\n{state.get('jobstreet_status')}\n\nDatabase\nCONNECTED\n\nDiscord\nCONNECTED",inline=True)
        embed.add_field(name='𝐒𝐂𝐀𝐍𝐍𝐈𝐍𝐆 𝐍𝐎𝐖' if scanning else '𝐒𝐂𝐀𝐍 𝐂𝐎𝐌𝐏𝐋𝐄𝐓𝐄',value='Checking recent active jobs…' if scanning else f"Jobs checked: {state.get('jobs_checked',0)}\nNew qualifying jobs: {state.get('new_recent_jobs',0)}\nAlerts sent: {state.get('alerts_sent',0)}",inline=False)
        embed.add_field(name='𝐇𝐎𝐖 𝐓𝐎 𝐔𝐒𝐄',value='Use **SEARCH JOBS**, type `v!search Junior DevOps`, or type `v!resume` to replace the saved resume.',inline=False)
        set_bot_footer(embed)
        return embed
    class ReviewView(discord.ui.View):
        def __init__(self, job_id): super().__init__(timeout=900); self.job_id=job_id
        async def letter(self):
            from .models import Job
            def load():
                with repo.sessions() as s: return s.get(Job,self.job_id)
            job=await asyncio.to_thread(load)
            return job,(job.raw_metadata or {}).get('cover_letter','') if job else ''
        @discord.ui.button(label='VIEW COVER LETTER',style=discord.ButtonStyle.secondary)
        async def view_letter(self,interaction,button):
            await interaction.response.defer(ephemeral=True,thinking=True)
            job,letter=await self.letter()
            if not letter: await interaction.followup.send('No cover letter is available.',ephemeral=True); return
            chunks=[letter[i:i+3900] for i in range(0,len(letter),3900)]
            await interaction.followup.send(embed=styled_embed('𝐂𝐎𝐕𝐄𝐑 𝐋𝐄𝐓𝐓𝐄𝐑',chunks[0]),ephemeral=True)
            for chunk in chunks[1:]: await interaction.followup.send(embed=styled_embed('𝐂𝐎𝐕𝐄𝐑 𝐋𝐄𝐓𝐓𝐄𝐑',chunk),ephemeral=True)
        @discord.ui.button(label='REGENERATE',style=discord.ButtonStyle.secondary)
        async def regenerate(self,interaction,button):
            await interaction.response.defer(ephemeral=True,thinking=True); _,_,mode=await prepare_letter(self.job_id,True,True); await interaction.followup.send(f'Cover letter regenerated with {mode}.',ephemeral=True)
        @discord.ui.button(label='USE TEMPLATE',style=discord.ButtonStyle.secondary)
        async def template(self,interaction,button):
            await interaction.response.defer(ephemeral=True,thinking=True); _,_,mode=await prepare_letter(self.job_id,False,True); await interaction.followup.send(f'Cover letter set to {mode}.',ephemeral=True)
        @discord.ui.button(label='VIEW RESUME',style=discord.ButtonStyle.secondary)
        async def resume(self,interaction,button):
            await interaction.response.defer(ephemeral=True,thinking=True)
            stored=await asyncio.to_thread(repo.resume_record)
            if stored:
                await interaction.followup.send(file=discord.File(io.BytesIO(stored['file_data']),filename=stored['filename']),ephemeral=True)
                return
            path=Path(cfg.resume_path) if cfg.resume_path else None
            if not path or not path.exists(): await interaction.followup.send('Private resume file is not configured on the service.',ephemeral=True); return
            data=await asyncio.to_thread(path.read_bytes)
            await interaction.followup.send(file=discord.File(io.BytesIO(data),filename='Mark_Andrei_Castillo_Resume.pdf'),ephemeral=True)
        @discord.ui.button(label='SEND APPLICATION',style=discord.ButtonStyle.primary)
        async def send(self,interaction,button):
            from .models import Job
            await interaction.response.defer(ephemeral=True,thinking=True)
            def load():
                with repo.sessions() as s: return s.get(Job,self.job_id)
            job=await asyncio.to_thread(load)
            if not job: await interaction.followup.send('Job not found.',ephemeral=True); return
            if job.status=='APPLIED': await interaction.followup.send('Already applied. No duplicate application will be sent.',ephemeral=True); return
            view=ConfirmSendView(self.job_id)
            await interaction.followup.send(embed=styled_embed('𝐂𝐎𝐍𝐅𝐈𝐑𝐌 𝐀𝐏𝐏𝐋𝐈𝐂𝐀𝐓𝐈𝐎𝐍',f'**{job.company}**\n{job.title}\n\nResume attached: configured on send\nCover letter attached: ready\n\nDry run: {"ON" if cfg.application_dry_run else "OFF"}'),view=view,ephemeral=True)
        @discord.ui.button(label='CANCEL',style=discord.ButtonStyle.secondary)
        async def cancel(self,interaction,button): await interaction.response.send_message('Application review cancelled.',ephemeral=True)
    class ConfirmSendView(discord.ui.View):
        def __init__(self,job_id): super().__init__(timeout=600); self.job_id=job_id
        @discord.ui.button(label='CONFIRM SEND',style=discord.ButtonStyle.danger)
        async def confirm(self,interaction,button):
            from .models import Job
            await interaction.response.defer(ephemeral=True,thinking=True)
            def check_and_record():
                with repo.sessions() as s:
                    job=s.get(Job,self.job_id)
                    if not job: return 'missing'
                    if job.status=='APPLIED': return 'applied'
                    if cfg.application_dry_run:
                        job.raw_metadata={**(job.raw_metadata or {}),'application_dry_run_at':datetime.now().isoformat()}
                        s.commit()
                        return 'dry_run'
                    return 'live_disabled'
            result=await asyncio.to_thread(check_and_record)
            if result=='missing': await interaction.followup.send('Job not found.',ephemeral=True); return
            if result=='applied': await interaction.followup.send('Already applied. No duplicate application will be sent.',ephemeral=True); return
            if result=='dry_run': await interaction.followup.send('Dry run complete. No employer was contacted.',ephemeral=True); return
            await interaction.followup.send('Sending is not enabled for live applications.',ephemeral=True)
        @discord.ui.button(label='CANCEL',style=discord.ButtonStyle.secondary)
        async def cancel(self,interaction,button): await interaction.response.send_message('Send cancelled.',ephemeral=True)
    class JobView(discord.ui.View):
        def __init__(self, job):
            super().__init__(timeout=900); self.job_id=job.id
            self.add_item(discord.ui.Button(label='VIEW JOB',style=discord.ButtonStyle.link,url=job.url))
        @discord.ui.button(label='APPLY NOW',style=discord.ButtonStyle.primary)
        async def apply(self,interaction,button):
            await interaction.response.defer(ephemeral=True,thinking=True)
            job,_,mode=await prepare_letter(self.job_id,True)
            if not job: await interaction.followup.send('This job is no longer available.',ephemeral=True); return
            embed=styled_embed('𝐀𝐏𝐏𝐋𝐈𝐂𝐀𝐓𝐈𝐎𝐍 𝐑𝐄𝐕𝐈𝐄𝐖',f'**{job.title}**\n{job.company}')
            stored_resume=await asyncio.to_thread(repo.resume_info)
            embed.add_field(name='Resume',value='READY' if stored_resume or (cfg.resume_path and Path(cfg.resume_path).exists()) else 'NOT CONFIGURED',inline=True)
            embed.add_field(name='Cover Letter',value=f'GENERATED WITH {mode}',inline=True)
            embed.add_field(name='Application Method',value='Email' if job.application_email else 'Employer portal',inline=False)
            await interaction.followup.send(embed=embed,view=ReviewView(self.job_id),ephemeral=True)
        @discord.ui.button(label='SAVE',style=discord.ButtonStyle.secondary)
        async def save(self,interaction,button):
            from .models import Job
            await interaction.response.defer(ephemeral=True,thinking=True)
            def save_job():
                with repo.sessions() as s:
                    job=s.get(Job,self.job_id)
                    if not job: return False
                    job.status='SAVED'; s.commit(); return True
            if not await asyncio.to_thread(save_job):
                await interaction.followup.send('This job is no longer available.',ephemeral=True); return
            embed=interaction.message.embeds[0]; embed.add_field(name='𝐒𝐓𝐀𝐓𝐔𝐒',value='SAVED',inline=False)
            await interaction.message.edit(embed=embed,view=self)
            await interaction.followup.send(embed=styled_embed('𝐒𝐀𝐕𝐄𝐃','Saved for later.'),ephemeral=True)
        @discord.ui.button(label='SKIP',style=discord.ButtonStyle.secondary)
        async def skip(self,interaction,button):
            from .models import Job
            await interaction.response.defer(ephemeral=True,thinking=True)
            def skip_job():
                with repo.sessions() as s:
                    job=s.get(Job,self.job_id)
                    if not job: return False
                    job.status='IGNORED'; s.commit(); return True
            if not await asyncio.to_thread(skip_job):
                await interaction.followup.send('This job is no longer available.',ephemeral=True); return
            embed=interaction.message.embeds[0]; embed.add_field(name='𝐒𝐓𝐀𝐓𝐔𝐒',value='SKIPPED',inline=False)
            for child in self.children:
                if getattr(child,'label',None) in ('APPLY NOW','SAVE','SKIP'): child.disabled=True
            await interaction.message.edit(embed=embed,view=self)
            await interaction.followup.send(embed=styled_embed('𝐒𝐊𝐈𝐏𝐏𝐄𝐃','Future alerts for this vacancy are stopped.'),ephemeral=True)
    def card(job):
        embed=styled_embed('𝐇𝐈𝐆𝐇 𝐌𝐀𝐓𝐂𝐇' if job.score>=85 else '𝐄𝐍𝐓𝐑𝐘-𝐋𝐄𝐕𝐄𝐋 𝐓𝐄𝐂𝐇',f'**{job.score}% MATCH**\n\n**{job.title}**\n{job.company}\n{job.location}',url=job.url)
        if job.match_reasons: embed.add_field(name='𝐖𝐇𝐘 𝐈𝐓 𝐅𝐈𝐓𝐒',value='\n'.join(job.match_reasons[:5]),inline=False)
        return embed,JobView(job)
    async def send_jobs(interaction,jobs):
        if not jobs: await interaction.followup.send('No recent qualifying jobs found.',ephemeral=True); return
        for job in jobs[:3]:
            embed,view=card(job); await interaction.followup.send(embed=embed,view=view,ephemeral=True)
    async def help_response(interaction):
        embed=styled_embed('𝐇𝐎𝐖 𝐓𝐎 𝐔𝐒𝐄','`v!search <role>` — latest roles\n`v!latest` — newest stored jobs\n`v!status` — monitor health\n`v!scan` — one protected scan\n`v!help` — this guide')
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
        def __init__(self):
            super().__init__(timeout=None)
            if cfg.public_base_url:
                token=ActionTokens(cfg).issue_control('resume')
                if token: self.add_item(discord.ui.Button(label='UPLOAD RESUME',style=discord.ButtonStyle.link,url=f'{cfg.public_base_url.rstrip("/")}/resume/{token}'))
        @discord.ui.button(label='SCAN NOW',style=discord.ButtonStyle.primary,custom_id='jobhunter:scan')
        async def scan(self,interaction,button):
            if scan_task and not scan_task.done(): await interaction.response.send_message(embed=styled_embed('𝐒𝐂𝐀𝐍𝐍𝐈𝐍𝐆','A scan is already running.'),ephemeral=True); return
            await interaction.response.send_message(embed=styled_embed('𝐒𝐂𝐀𝐍𝐍𝐈𝐍𝐆','Searching for recent active jobs…'),ephemeral=True)
            await launch_scan()
        @discord.ui.button(label='SEARCH JOBS',style=discord.ButtonStyle.secondary,custom_id='jobhunter:search')
        async def search(self,interaction,button): await interaction.response.send_modal(SearchModal())
        @discord.ui.button(label='VIEW STATUS',style=discord.ButtonStyle.secondary,custom_id='jobhunter:status')
        async def status(self,interaction,button):
            await interaction.response.defer(ephemeral=True,thinking=True)
            await interaction.followup.send(embed=await panel_embed(),ephemeral=True)
        @discord.ui.button(label='VIEW LATEST JOBS',style=discord.ButtonStyle.secondary,custom_id='jobhunter:latest')
        async def latest(self,interaction,button):
            from sqlalchemy import select
            from .models import Job, JobStatus
            await interaction.response.defer(ephemeral=True,thinking=True)
            def load_latest():
                with repo.sessions() as s:
                    return s.scalars(select(Job).where(Job.status!=JobStatus.EXPIRED.value,Job.score>=cfg.min_notify_score).order_by(Job.date_posted.desc()).limit(3)).all()
            jobs=await asyncio.to_thread(load_latest)
            await send_jobs(interaction,jobs)
        @discord.ui.button(label='HELP',style=discord.ButtonStyle.secondary,custom_id='jobhunter:help')
        async def help(self,interaction,button): await help_response(interaction)
    async def resolve_channel():
        nonlocal panel_channel
        if panel_channel: return panel_channel
        try:
            if cfg.discord_control_channel_id:
                channel_id=int(cfg.discord_control_channel_id)
                panel_channel=bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            elif cfg.discord_webhook_url:
                hook=discord.Webhook.from_url(cfg.discord_webhook_url,client=bot); fetched=await hook.fetch()
                panel_channel=bot.get_channel(fetched.channel_id) or await bot.fetch_channel(fetched.channel_id)
        except Exception as exc: log.warning('discord_control_channel_unavailable',extra={'error':str(exc)})
        return panel_channel
    async def refresh_panel():
        channel=await resolve_channel()
        if not channel: return
        message_id=await asyncio.to_thread(repo.state,'discord_bot_control_panel_message_id')
        embed=await panel_embed()
        if message_id:
            try:
                message=await channel.fetch_message(int(message_id)); await message.edit(embed=embed,view=panel_view); return
            except Exception: await asyncio.to_thread(repo.set_state,'discord_bot_control_panel_message_id',None)
        message=await channel.send(embed=embed,view=panel_view)
        await asyncio.to_thread(repo.set_state,'discord_bot_control_panel_message_id',str(message.id))
    async def launch_scan(message=None):
        nonlocal scan_task
        if scan_task and not scan_task.done(): return False
        async def background():
            started=time.monotonic()
            try:
                outcomes=await manual_scan(); state=await asyncio.to_thread(scheduler_snapshot); duration=int(time.monotonic()-started)
                text=f"𝐒𝐂𝐀𝐍 𝐂𝐎𝐌𝐏𝐋𝐄𝐓𝐄\nDuration: {duration}s\nSources: {len(outcomes)}\nJobs checked: {state.get('jobs_checked',0)}\nNew matches: {state.get('new_recent_jobs',0)}\nAlerts sent: {state.get('alerts_sent',0)}"
            except Exception as exc: text=str(getattr(exc,'detail','Scan unavailable.'))
            await refresh_panel()
            if message:
                try: await message.edit(content=None,embed=styled_embed('𝐒𝐂𝐀𝐍 𝐂𝐎𝐌𝐏𝐋𝐄𝐓𝐄',text))
                except Exception: pass
        scan_task=asyncio.create_task(background()); return True
    async def deliver_pending_alerts():
        from sqlalchemy import select
        from .models import Job, JobStatus
        channel=await resolve_channel()
        if not channel: return
        def pending_ids():
            with repo.sessions() as s:
                return s.scalars(select(Job.id).where(Job.notification_state=='BOT_PENDING').limit(3)).all()
        ids=await asyncio.to_thread(pending_ids)
        for job_id in ids:
            def load_job():
                with repo.sessions() as s: return s.get(Job,job_id)
            job=await asyncio.to_thread(load_job)
            if not job: continue
            try:
                embed,view=card(job); await channel.send(content=random.choice(cfg.discord_motivations),embed=embed,view=view)
                def mark_sent():
                    with repo.sessions() as s:
                        stored=s.get(Job,job_id)
                        if stored:
                            stored.notification_state='SENT'; stored.status=JobStatus.NOTIFIED.value; s.commit()
                await asyncio.to_thread(mark_sent)
            except Exception as exc: log.warning('discord_bot_alert_failed',extra={'job_id':job_id,'error':str(exc)})
    async def panel_watcher():
        last=None
        while not bot.is_closed():
            state=await asyncio.to_thread(scheduler_snapshot); marker=(state.get('phase'),state.get('last_poll_at'),state.get('next_poll_at'))
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
        await asyncio.to_thread(repo.set_state,'discord_bot_health',{'healthy':True,'started_once':True})
        log.info('discord_bot_ready')
    @bot.event
    async def on_message(message):
        if message.author.bot or not message.content.lower().startswith('v!'): return
        command,_,argument=message.content[2:].strip().partition(' '); command=command.lower(); argument=argument.strip()
        if command=='help':
            embed=styled_embed('𝐇𝐎𝐖 𝐓𝐎 𝐔𝐒𝐄','`v!search <role>`\n`v!latest`\n`v!status`\n`v!scan`\n`v!help`'); await message.channel.send(embed=embed); return
        if command=='resume':
            token=ActionTokens(cfg).issue_control('resume')
            if not token or not cfg.public_base_url:
                await message.channel.send('Resume upload is not configured. Set APP_SECRET_KEY and PUBLIC_BASE_URL first.'); return
            await message.channel.send(f'Upload or replace the saved resume here: {cfg.public_base_url.rstrip("/")}/resume/{token}'); return
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
            def load_latest():
                with repo.sessions() as s:
                    return s.scalars(select(Job).where(Job.status!=JobStatus.EXPIRED.value,Job.score>=cfg.min_notify_score).order_by(Job.date_posted.desc()).limit(3)).all()
            jobs=await asyncio.to_thread(load_latest)
            for job in jobs:
                embed,view=card(job); await message.channel.send(embed=embed,view=view)
        elif command=='status': await message.channel.send(embed=await panel_embed())
        elif command=='scan':
            if scan_task and not scan_task.done(): await message.channel.send(embed=styled_embed('𝐒𝐂𝐀𝐍𝐍𝐈𝐍𝐆','A scan is already running.')); return
            notice=await message.channel.send(embed=styled_embed('𝐒𝐂𝐀𝐍𝐍𝐈𝐍𝐆','Searching for recent active jobs…')); await launch_scan(notice)
        else: await message.channel.send('Use `v!help`.')
    try: await bot.start(cfg.discord_bot_token)
    finally:
        await asyncio.to_thread(repo.set_state,'discord_bot_health',{'healthy':False,'started_once':True})
        if panel_task: panel_task.cancel()
        if not bot.is_closed(): await bot.close()
