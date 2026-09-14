from __future__ import annotations
import asyncio, io, logging, os, random, socket, time, uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from .security import ActionTokens

log=logging.getLogger(__name__)
#FF7F00 is the warm orange used by the approved server reference message.
MASTER_EMBED_COLOUR=0xFF7F00
MASTER_EMBED_FOOTER='After Hours Job Hunter • Made by masoncalix'


def make_after_hours_embed(title: str, description: str = '', url: str | None = None, colour: int = MASTER_EMBED_COLOUR, footer_icon_url: str | None = None):
    """The one production visual factory for every bot embed and UI preview."""
    import discord
    kwargs={'title':title,'description':description,'colour':colour}
    if url:
        kwargs['url']=url
    embed=discord.Embed(**kwargs)
    footer={'text':MASTER_EMBED_FOOTER}
    if footer_icon_url:
        footer['icon_url']=footer_icon_url
    embed.set_footer(**footer)
    return embed

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
    bot=discord.Client(intents=intents); panel_channel=None; panel_view=None; panel_task=None; scan_task=None; registered=False; search_cooldowns={}; active_searches=set(); processed_messages={}; lease_task=None
    # A token can only have one useful gateway consumer.  The shared database
    # lease prevents a local dev server from replying alongside Render.
    instance_id=f"{os.getenv('RENDER_SERVICE_ID') or socket.gethostname()}-{uuid.uuid4().hex[:10]}"
    acquired=await asyncio.to_thread(repo.acquire_discord_bot_lease,instance_id)
    if not acquired:
        log.info('discord_bot_inactive_another_instance_owns_gateway')
        return
    log.info('discord_bot_lease_acquired')

    async def renew_lease():
        while not bot.is_closed():
            await asyncio.sleep(20)
            if not await asyncio.to_thread(repo.renew_discord_bot_lease,instance_id):
                log.warning('discord_bot_lease_lost'); await bot.close(); return

    @asynccontextmanager
    async def typing(channel):
        indicator=getattr(channel,'typing',None)
        if not indicator:
            yield; return
        async with indicator():
            yield

    def search_embed(role, progress, title='𝐒𝐄𝐀𝐑𝐂𝐇𝐈𝐍𝐆', detail=''):
        text=(
            f"**{role.title()}**\nPhilippines · Recent jobs\n\n"
            f"Sources checked: {progress.sources_checked} / {progress.sources_total}\n"
            f"Jobs reviewed: {progress.jobs_reviewed}\n"
            f"Recent 0–90 day jobs: {progress.recent_ph_jobs}\n"
            f"Query-relevant jobs: {progress.query_relevant_jobs}\n"
            f"Entry-level compatible: {progress.entry_level_compatible}\n"
            f"Qualifying matches: {progress.potential_matches}"
        )
        if detail: text += f"\n\n{detail}"
        return styled_embed(title,text)

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
        # Discord renders this as an icon; the URL is never placed in message text.
        avatar=getattr(getattr(bot,'user',None),'display_avatar',None)
        footer={'text':MASTER_EMBED_FOOTER}
        if avatar: footer['icon_url']=str(avatar.url)
        embed.set_footer(**footer)
        return embed
    def styled_embed(title,description='',url=None,colour=MASTER_EMBED_COLOUR):
        avatar=getattr(getattr(bot,'user',None),'display_avatar',None)
        return make_after_hours_embed(title,description,url,colour,str(avatar.url) if avatar else None)
    async def panel_embed():
        state=await asyncio.to_thread(scheduler_snapshot); scanning=state.get('phase')=='scanning'
        embed=styled_embed('𝐀𝐅𝐓𝐄𝐑 𝐇𝐎𝐔𝐑𝐒 𝐉𝐎𝐁 𝐇𝐔𝐍𝐓𝐄𝐑','Your automated Philippine tech-job monitor is online.')
        embed.add_field(name='𝐒𝐘𝐒𝐓𝐄𝐌 𝐒𝐓𝐀𝐓𝐔𝐒',value=f"Service\nONLINE\n\nScheduler\n{state.get('status','starting').upper()}\n\nLast scan\n{discord_timestamp(state.get('last_poll_at'))}\n\nNext scan\n{discord_timestamp(state.get('next_poll_at'))}",inline=True)
        embed.add_field(name='𝐂𝐎𝐍𝐍𝐄𝐂𝐓𝐈𝐎𝐍𝐒',value=f"Sources\n{state.get('sources_working',0)} active\n\nLinkedIn\n{state.get('linkedin_status')}\n\nJobStreet\n{state.get('jobstreet_status')}\n\nDatabase\nCONNECTED\n\nDiscord\nCONNECTED",inline=True)
        embed.add_field(name='𝐒𝐂𝐀𝐍𝐍𝐈𝐍𝐆 𝐍𝐎𝐖' if scanning else '𝐒𝐂𝐀𝐍 𝐂𝐎𝐌𝐏𝐋𝐄𝐓𝐄',value='Checking recent active jobs…' if scanning else f"Jobs checked: {state.get('jobs_checked',0)}\nNew qualifying jobs: {state.get('new_recent_jobs',0)}\nAlerts sent: {state.get('alerts_sent',0)}",inline=False)
        embed.add_field(name='𝐇𝐎𝐖 𝐓𝐎 𝐔𝐒𝐄',value='Use **SEARCH JOBS** to look for a role, `v!scan` to start one protected scan, or `v!resume` to replace your saved resume.',inline=False)
        set_bot_footer(embed)
        return embed
    def application_method(job):
        return 'EMAIL' if job.application_email else ('PORTAL' if job.application_url else 'MANUAL REVIEW')
    def document_ready():
        return 'READY'
    async def send_letter_download(interaction, job, letter, extension):
        from .applications import cover_letter_filename, cover_letter_pdf_bytes, cover_letter_text_bytes
        data=cover_letter_text_bytes(letter) if extension=='txt' else cover_letter_pdf_bytes(letter)
        await interaction.followup.send(file=discord.File(io.BytesIO(data),filename=cover_letter_filename(job,extension)),ephemeral=True)
    class LetterPreviewView(discord.ui.View):
        def __init__(self, job_id): super().__init__(timeout=900); self.job_id=job_id
        async def letter(self):
            def load():
                from .models import Job
                with repo.sessions() as s: return s.get(Job,self.job_id)
            job=await asyncio.to_thread(load)
            return job,(job.raw_metadata or {}).get('cover_letter','') if job else ''
        @discord.ui.button(label='DOWNLOAD TXT',style=discord.ButtonStyle.secondary)
        async def download_txt(self,interaction,button):
            await interaction.response.defer(ephemeral=True,thinking=True); job,letter=await self.letter()
            if not job or not letter: await interaction.followup.send(embed=styled_embed('𝐃𝐎𝐂𝐔𝐌𝐄𝐍𝐓 𝐔𝐍𝐀𝐕𝐀𝐈𝐋𝐀𝐁𝐋𝐄','No saved cover letter is available.'),ephemeral=True); return
            await send_letter_download(interaction,job,letter,'txt')
        @discord.ui.button(label='DOWNLOAD PDF',style=discord.ButtonStyle.secondary)
        async def download_pdf(self,interaction,button):
            await interaction.response.defer(ephemeral=True,thinking=True); job,letter=await self.letter()
            if not job or not letter: await interaction.followup.send(embed=styled_embed('𝐃𝐎𝐂𝐔𝐌𝐄𝐍𝐓 𝐔𝐍𝐀𝐕𝐀𝐈𝐋𝐀𝐁𝐋𝐄','No saved cover letter is available.'),ephemeral=True); return
            await send_letter_download(interaction,job,letter,'pdf')
        @discord.ui.button(label='REGENERATE',style=discord.ButtonStyle.secondary)
        async def regenerate(self,interaction,button):
            await interaction.response.defer(ephemeral=True,thinking=True); _,_,mode=await prepare_letter(self.job_id,True,True)
            await interaction.followup.send(embed=styled_embed('𝐂𝐎𝐕𝐄𝐑 𝐋𝐄𝐓𝐓𝐄𝐑 𝐔𝐏𝐃𝐀𝐓𝐄𝐃',f'Your validated {mode.lower()} version is ready to review.'),ephemeral=True)
        @discord.ui.button(label='BACK',style=discord.ButtonStyle.secondary)
        async def back(self,interaction,button):
            await interaction.response.send_message(embed=styled_embed('𝐀𝐏𝐏𝐋𝐈𝐂𝐀𝐓𝐈𝐎𝐍 𝐑𝐄𝐕𝐈𝐄𝐖','You can return to the private application review above.'),ephemeral=True)
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
            from .applications import discord_cover_letter_chunks
            chunks=discord_cover_letter_chunks(letter)
            if not chunks:
                await interaction.followup.send('The saved cover letter contains no readable text.',ephemeral=True)
                return
            title=f'**{job.title}**\n{job.company}\n\n'
            await interaction.followup.send(embed=styled_embed('𝐂𝐎𝐕𝐄𝐑 𝐋𝐄𝐓𝐓𝐄𝐑',title+chunks[0]),view=LetterPreviewView(self.job_id),ephemeral=True)
            for index,chunk in enumerate(chunks[1:],start=2): await interaction.followup.send(embed=styled_embed(f'𝐂𝐎𝐕𝐄𝐑 𝐋𝐄𝐓𝐓𝐄𝐑 ({index}/{len(chunks)})',chunk),ephemeral=True)
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
        @discord.ui.button(label='DOWNLOAD LETTER',style=discord.ButtonStyle.secondary)
        async def download_letter(self,interaction,button):
            await interaction.response.defer(ephemeral=True,thinking=True)
            job,letter=await self.letter()
            if not job or not letter: await interaction.followup.send(embed=styled_embed('𝐃𝐎𝐂𝐔𝐌𝐄𝐍𝐓 𝐔𝐍𝐀𝐕𝐀𝐈𝐋𝐀𝐁𝐋𝐄','No saved cover letter is available.'),ephemeral=True); return
            await send_letter_download(interaction,job,letter,'txt')
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
            embed=styled_embed('𝐂𝐎𝐍𝐅𝐈𝐑𝐌 𝐀𝐏𝐏𝐋𝐈𝐂𝐀𝐓𝐈𝐎𝐍',f'**{job.title}**\n{job.company}')
            embed.add_field(name='𝐃𝐎𝐂𝐔𝐌𝐄𝐍𝐓𝐒',value=f'Resume\n{document_ready()}\n\nCover Letter\n{document_ready()}',inline=True)
            embed.add_field(name='𝐀𝐏𝐏𝐋𝐈𝐂𝐀𝐓𝐈𝐎𝐍 𝐌𝐄𝐓𝐇𝐎𝐃',value=application_method(job),inline=True)
            embed.add_field(name='𝐒𝐀𝐅𝐄𝐓𝐘',value=f'Dry Run\n{"ON" if cfg.application_dry_run else "OFF"}\n\nNo employer will be contacted while dry-run mode is enabled.' if cfg.application_dry_run else 'Live sending is disabled pending explicit configuration.',inline=False)
            await interaction.followup.send(embed=embed,view=view,ephemeral=True)
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
                        from .models import JobEvent
                        s.add(JobEvent(job_id=job.id,event_type='DRY_RUN_PACKAGE_PREPARED',detail='Application package prepared; no employer contacted.'))
                        s.commit()
                        return 'dry_run'
                    return 'live_disabled'
            result=await asyncio.to_thread(check_and_record)
            if result=='missing': await interaction.followup.send('Job not found.',ephemeral=True); return
            if result=='applied': await interaction.followup.send('Already applied. No duplicate application will be sent.',ephemeral=True); return
            if result=='dry_run':
                def load():
                    with repo.sessions() as s: return s.get(Job,self.job_id)
                job=await asyncio.to_thread(load)
                embed=styled_embed('𝐃𝐑𝐘 𝐑𝐔𝐍 𝐂𝐎𝐌𝐏𝐋𝐄𝐓𝐄',f'**{job.title}**\n{job.company}\n\nApplication package prepared successfully.')
                embed.add_field(name='𝐃𝐎𝐂𝐔𝐌𝐄𝐍𝐓𝐒',value='Resume\nREADY\n\nCover Letter\nREADY',inline=True)
                embed.add_field(name='𝐄𝐌𝐏𝐋𝐎𝐘𝐄𝐑 𝐂𝐎𝐍𝐓𝐀𝐂𝐓𝐄𝐃',value='NO',inline=True)
                embed.add_field(name='𝐒𝐀𝐅𝐄𝐓𝐘',value='This was a simulation only.\nNo application was sent.',inline=False)
                await interaction.followup.send(embed=embed,ephemeral=True); return
            await interaction.followup.send(embed=styled_embed('𝐒𝐄𝐍𝐃𝐈𝐍𝐆 𝐃𝐈𝐒𝐀𝐁𝐋𝐄𝐃','Live sending is not enabled for this application method.'),ephemeral=True)
        @discord.ui.button(label='BACK',style=discord.ButtonStyle.secondary)
        async def back(self,interaction,button): await interaction.response.send_message(embed=styled_embed('𝐀𝐏𝐏𝐋𝐈𝐂𝐀𝐓𝐈𝐎𝐍 𝐑𝐄𝐕𝐈𝐄𝐖','Return to the private review above.'),ephemeral=True)
        @discord.ui.button(label='CANCEL',style=discord.ButtonStyle.secondary)
        async def cancel(self,interaction,button): await interaction.response.send_message('Send cancelled.',ephemeral=True)
    async def send_application_review(interaction, job_id):
        job,_,mode=await prepare_letter(job_id,True)
        if not job:
            await interaction.followup.send('This job is no longer available.',ephemeral=True)
            return
        embed=styled_embed('𝐀𝐏𝐏𝐋𝐈𝐂𝐀𝐓𝐈𝐎𝐍 𝐑𝐄𝐕𝐈𝐄𝐖',f'**{job.title}**\n{job.company}')
        embed.add_field(name='𝐌𝐀𝐓𝐂𝐇',value=f'{job.score}% MATCH',inline=True)
        stored_resume=await asyncio.to_thread(repo.resume_info)
        embed.add_field(name='𝐑𝐄𝐒𝐔𝐌𝐄',value='READY' if stored_resume or (cfg.resume_path and Path(cfg.resume_path).exists()) else 'NOT CONFIGURED',inline=True)
        embed.add_field(name='𝐂𝐎𝐕𝐄𝐑 𝐋𝐄𝐓𝐓𝐄𝐑',value=f'READY ({mode})',inline=True)
        embed.add_field(name='𝐀𝐏𝐏𝐋𝐈𝐂𝐀𝐓𝐈𝐎𝐍 𝐌𝐄𝐓𝐇𝐎𝐃',value=application_method(job),inline=True)
        embed.add_field(name='𝐒𝐀𝐅𝐄𝐓𝐘',value='DRY RUN ON' if cfg.application_dry_run else 'MANUAL REVIEW REQUIRED',inline=False)
        await interaction.followup.send(embed=embed,view=ReviewView(job_id),ephemeral=True)
    class JobView(discord.ui.View):
        def __init__(self, job):
            super().__init__(timeout=900); self.job_id=job.id
            self.add_item(discord.ui.Button(label='VIEW JOB',style=discord.ButtonStyle.link,url=job.url))
        @discord.ui.button(label='APPLY NOW',style=discord.ButtonStyle.primary)
        async def apply(self,interaction,button):
            await interaction.response.defer(ephemeral=True,thinking=True)
            await send_application_review(interaction,self.job_id)
        @discord.ui.button(label='SAVE',style=discord.ButtonStyle.secondary)
        async def save(self,interaction,button):
            from .models import Job
            await interaction.response.defer(ephemeral=True,thinking=True)
            if not await asyncio.to_thread(repo.set_job_status,self.job_id,'SAVED','Saved from Discord job card.'):
                await interaction.followup.send('This job is no longer available.',ephemeral=True); return
            embed=interaction.message.embeds[0]; embed.add_field(name='𝐒𝐓𝐀𝐓𝐔𝐒',value='SAVED',inline=False)
            await interaction.message.edit(embed=embed,view=self)
            await interaction.followup.send(embed=styled_embed('𝐒𝐀𝐕𝐄𝐃','This job has been saved for later.'),ephemeral=True)
        @discord.ui.button(label='SKIP',style=discord.ButtonStyle.secondary)
        async def skip(self,interaction,button):
            from .models import Job
            await interaction.response.defer(ephemeral=True,thinking=True)
            if not await asyncio.to_thread(repo.set_job_status,self.job_id,'IGNORED','Skipped from Discord job card.'):
                await interaction.followup.send('This job is no longer available.',ephemeral=True); return
            embed=interaction.message.embeds[0]; embed.add_field(name='𝐒𝐓𝐀𝐓𝐔𝐒',value='SKIPPED',inline=False)
            for child in self.children:
                if getattr(child,'label',None) in ('APPLY NOW','SAVE','SKIP'): child.disabled=True
            await interaction.message.edit(embed=embed,view=self)
            await interaction.followup.send(embed=styled_embed('𝐒𝐊𝐈𝐏𝐏𝐄𝐃','You will no longer receive alerts for this vacancy.'),ephemeral=True)
    def card(job):
        embed=styled_embed('𝐇𝐈𝐆𝐇 𝐌𝐀𝐓𝐂𝐇' if job.score>=85 else '𝐄𝐍𝐓𝐑𝐘-𝐋𝐄𝐕𝐄𝐋 𝐓𝐄𝐂𝐇',f'**{job.score}% MATCH**\n\n**{job.title}**\n{job.company}\n{job.location}',url=job.url)
        if job.match_reasons: embed.add_field(name='𝐖𝐇𝐘 𝐈𝐓 𝐅𝐈𝐓𝐒',value='\n'.join(job.match_reasons[:5]),inline=False)
        return embed,JobView(job)
    async def send_jobs(interaction,jobs):
        if not jobs: await interaction.followup.send('No recent qualifying jobs found.',ephemeral=True); return
        for job in jobs[:3]:
            embed,view=card(job); await interaction.followup.send(embed=embed,view=view,ephemeral=True)
    async def load_view_all_jobs(page: int, page_size: int = 5, days: int = 90, min_score: int = 0):
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import case, func, select
        from .models import Job, JobStatus
        cutoff=datetime.now(timezone.utc)-timedelta(days=max(1,min(90,int(days))))
        def load():
            with repo.sessions() as s:
                source_quality=case(
                    (Job.source.like('greenhouse:%'),3),
                    (Job.source.like('ashby:%'),3),
                    (Job.source.like('lever:%'),3),
                    (Job.source.like('brightdata:%'),2),
                    (Job.source.like('jobstreet:%'),2),
                    else_=1,
                )
                base=select(Job).where(
                    Job.status!=JobStatus.EXPIRED.value,
                    Job.date_posted.is_not(None),
                    Job.date_posted>=cutoff,
                    Job.score>=max(0,min(100,int(min_score))),
                )
                total=s.scalar(select(func.count()).select_from(base.subquery())) or 0
                rows=s.scalars(base.order_by(Job.date_posted.desc(),Job.score.desc(),source_quality.desc()).offset(page*page_size).limit(page_size)).all()
                return rows,total
        return await asyncio.to_thread(load)
    def view_all_embed(jobs, page, total):
        pages=max(1,(total+4)//5)
        lines=[]
        for number,job in enumerate(jobs,start=page*5+1):
            posted=job.date_posted.strftime('%Y-%m-%d') if job.date_posted else 'Unknown date'
            age='unknown age'
            if job.date_posted:
                posted_at=job.date_posted
                if posted_at.tzinfo is None:
                    posted_at=posted_at.replace(tzinfo=timezone.utc)
                age=f'{max(0,(datetime.now(timezone.utc)-posted_at).days)}d old'
            location=(job.location or 'Location not stated').replace(', Philippines','').strip()
            source=job.source.replace(':',' / ')
            lines.append(
                f'`{number:02}  {posted}  ·  {age}  ·  {job.score}% MATCH`\n'
                f'**{job.title}**\n{job.company} · {location}\nSource: {source}'
            )
        body='\n\n'.join(lines) if lines else 'No recent qualifying jobs are stored yet.'
        embed=styled_embed('𝐉𝐎𝐁 𝐁𝐎𝐀𝐑𝐃',f'Active stored computer jobs from the last 90 days · newest/reposted first\n\n{body}')
        embed.add_field(
            name='𝐍𝐀𝐕𝐈𝐆𝐀𝐓𝐈𝐎𝐍',
            value=(
                f'Page {page+1} of {pages} · {total} stored matches\n'
                'Page changes are instant and use stored results only. '
                'Use SCAN NOW for a live refresh with progress.'
            ),
            inline=False,
        )
        return embed,pages
    class ViewAllApplyButton(discord.ui.Button):
        def __init__(self,job_id,index):
            super().__init__(label=f'APPLY NOW {index:02}',style=discord.ButtonStyle.primary,row=2)
            self.job_id=job_id
        async def callback(self,interaction):
            await interaction.response.defer(ephemeral=True,thinking=True)
            await send_application_review(interaction,self.job_id)
    class ViewAllJobsView(discord.ui.View):
        def __init__(self,jobs,page=0,total_pages=1):
            super().__init__(timeout=900); self.page=page; self.total_pages=total_pages
            for index,job in enumerate(jobs,start=page*5+1):
                url=job.application_url or job.url
                if url and url.startswith(('https://','http://')):
                    self.add_item(discord.ui.Button(label=f'OPEN {index:02}',style=discord.ButtonStyle.link,url=url,row=1))
                self.add_item(ViewAllApplyButton(job.id,index))
            self._sync()
        def _sync(self):
            self.previous.disabled=self.page<=0
            self.next.disabled=self.page>=self.total_pages-1
            self.indicator.label=f'PAGE {self.page+1} / {self.total_pages}'
        async def render(self,message):
            jobs,total=await load_view_all_jobs(self.page)
            pages=max(1,(total+4)//5)
            embed,_=view_all_embed(jobs,self.page,total)
            await message.edit(embed=embed,view=ViewAllJobsView(jobs,self.page,pages))
        @discord.ui.button(label='PREVIOUS',style=discord.ButtonStyle.secondary)
        async def previous(self,interaction,button):
            await interaction.response.defer()
            self.page=max(0,self.page-1); await self.render(interaction.message)
        @discord.ui.button(label='PAGE 1 / 1',style=discord.ButtonStyle.secondary,disabled=True)
        async def indicator(self,interaction,button): pass
        @discord.ui.button(label='NEXT',style=discord.ButtonStyle.primary)
        async def next(self,interaction,button):
            await interaction.response.defer()
            self.page=min(self.total_pages-1,self.page+1); await self.render(interaction.message)
    async def send_view_all(destination, ephemeral=False, page=0, days=90, min_score=0):
        send_options={}
        if ephemeral:
            send_options['ephemeral']=True
        loading=await destination.send(
            embed=styled_embed('𝐉𝐎𝐁 𝐁𝐎𝐀𝐑𝐃','Loading stored matches…'),
            **send_options,
        )
        try:
            jobs,total=await load_view_all_jobs(page=page, days=days, min_score=min_score)
            embed,pages=view_all_embed(jobs,page,total)
            await loading.edit(embed=embed,view=ViewAllJobsView(jobs,page,pages))
        except Exception as exc:
            log.warning('discord_view_all_failed',extra={'error_type':type(exc).__name__})
            await loading.edit(
                embed=styled_embed(
                    '𝐉𝐎𝐁 𝐁𝐎𝐀𝐑𝐃',
                    'The stored job board is temporarily unavailable. Please try again in a moment.',
                ),
                view=None,
            )
        return loading
    async def execute_search(channel, user_id, role, location='Philippines', freshness='Past 24 hours', work_setup='', minimum_score=0, ephemeral=False, interaction=None):
        """One status card, real counters, then shared job cards. Never blocks heartbeat."""
        now=time.time()
        if user_id in active_searches:
            return await channel.send('You already have a search in progress.')
        remaining=search_cooldowns.get(user_id,0)-now
        if remaining>0:
            return await channel.send(f'Please wait {max(1,int(remaining))} seconds before searching again.')
        active_searches.add(user_id); search_cooldowns[user_id]=now+30
        from .manual_search import SearchProgress
        progress=SearchProgress()
        status=await channel.send(embed=search_embed(role,progress,detail='Starting targeted discovery…'))
        last_edit=0.0
        async def update(current):
            nonlocal last_edit
            # Source completion is meaningful; coalesce rapid concurrent updates.
            if time.monotonic()-last_edit < .25 and current.sources_checked < current.sources_total: return
            last_edit=time.monotonic()
            await status.edit(embed=search_embed(role,current,detail='Checking live ATS sources…'))
        try:
            async with typing(channel):
                if hasattr(manual_search,'find_with_progress'):
                    outcome=await manual_search.find_with_progress(role,location,freshness,'',5,work_setup,max(0,min(100,int(minimum_score))),True,'all',update)
                    jobs=outcome.jobs; current=outcome.progress; duration=outcome.duration_seconds
                else:
                    jobs=await manual_search.find(role,location,freshness,'',5,work_setup,max(0,min(100,int(minimum_score))))
                    current=progress; duration=0
            if jobs:
                await status.edit(embed=search_embed(role,current,'𝐒𝐄𝐀𝐑𝐂𝐇 𝐂𝐎𝐌𝐏𝐋𝐄𝐓𝐄',f'Matching jobs: {len(jobs)}\nDuration: {duration:.1f}s'))
                for job in jobs[:3]:
                    embed,view=card(job); await channel.send(embed=embed,view=view)
            else:
                source_note='Live discovery was unavailable; cached data only was checked.' if not current.live_available else 'Try a related title or use fewer words in your search.'
                suggestions=await asyncio.to_thread(manual_search.suggested_recent_jobs,role,3)
                suggestion_note=(
                    f'Qualifying matches: 0\n\n{source_note}\n\n'
                    f'𝐒𝐔𝐆𝐆𝐄𝐒𝐓𝐄𝐃 𝐀𝐋𝐓𝐄𝐑𝐍𝐀𝐓𝐈𝐕𝐄𝐒: {len(suggestions)}'
                    if suggestions else f'Qualifying matches: 0\n\n{source_note}'
                )
                await status.edit(embed=search_embed(role,current,'𝐍𝐎 𝐑𝐄𝐂𝐄𝐍𝐓 𝐌𝐀𝐓𝐂𝐇𝐄𝐒',suggestion_note))
                if suggestions:
                    await channel.send(embed=styled_embed(
                        '𝐒𝐔𝐆𝐆𝐄𝐒𝐓𝐄𝐃 𝐉𝐎𝐁𝐒',
                        'No exact recent match was found. These are real recent jobs that fit your profile and may be worth checking.',
                    ))
                    for job in suggestions:
                        embed,view=card(job); await channel.send(embed=embed,view=view)
            return status
        except Exception:
            await status.edit(embed=styled_embed('𝐒𝐄𝐀𝐑𝐂𝐇 𝐔𝐍𝐀𝐕𝐀𝐈𝐋𝐀𝐁𝐋𝐄','We could not complete that search. Please try again in a moment.'))
            return status
        finally:
            active_searches.discard(user_id)
    async def help_response(interaction):
        embed=styled_embed('𝐇𝐎𝐖 𝐓𝐎 𝐔𝐒𝐄','`v!search <role>` — search for recent roles\n`v!latest` — view the newest saved matches\n`v!viewall` — browse all stored matches\n`v!status` — check monitor health\n`v!scan` — run one protected scan\n`v!jobstreet` — connect or manage JobStreet\n`v!help` — show this guide')
        await interaction.response.send_message(embed=embed,ephemeral=True)

    def jobstreet_status_embed(user_id):
        from .jobstreet_link import connection_status
        status=connection_status(cfg,repo,user_id)
        with repo.sessions() as session:
            from .models import SourceConnection
            from sqlalchemy import select
            record=session.scalar(select(SourceConnection).where(
                SourceConnection.source=='jobstreet',
                SourceConnection.discord_user_id==str(user_id),
            ))
        embed=styled_embed('𝐉𝐎𝐁𝐒𝐓𝐑𝐄𝐄𝐓',f'Authenticated source status: **{status}**')
        if record and record.last_verified_at:
            verified=record.last_verified_at.strftime('%Y-%m-%d %H:%M UTC')
            embed.add_field(name='𝐋𝐀𝐒𝐓 𝐕𝐄𝐑𝐈𝐅𝐈𝐄𝐃',value=verified,inline=False)
        if record and record.last_error:
            embed.add_field(name='𝐍𝐎𝐓𝐄',value=record.last_error,inline=False)
        embed.add_field(
            name='𝐒𝐀𝐅𝐄𝐓𝐘',
            value='Google sign-in, 2FA, security prompts, consent, and CAPTCHA stay manual. '
                  'Only the encrypted browser session is retained.',
            inline=False,
        )
        return embed

    async def send_jobstreet_link(interaction, user_id):
        from .jobstreet_link import create_request
        # The database write happens after Discord's three-second acknowledgement
        # window, just like scan/search work. Chromium itself runs on the
        # Windows PC that downloads the temporary connector.
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not cfg.public_base_url:
            await interaction.followup.send(
                embed=styled_embed('𝐉𝐎𝐁𝐒𝐓𝐑𝐄𝐄𝐓 𝐔𝐍𝐀𝐕𝐀𝐈𝐋𝐀𝐁𝐋𝐄',
                                   'Set PUBLIC_BASE_URL before creating a private setup link.'),
                ephemeral=True,
            )
            return
        token=await asyncio.to_thread(create_request,repo,user_id,600,cfg.app_secret_key)
        url=f'{cfg.public_base_url.rstrip("/")}/connect/jobstreet/{token}'
        embed=styled_embed(
            '𝐂𝐎𝐍𝐍𝐄𝐂𝐓 𝐉𝐎𝐁𝐒𝐓𝐑𝐄𝐄𝐓',
            'Open the private setup link on the Windows PC where you want to authenticate. '
            'Download and run the connector; it opens local Playwright Chromium automatically.',
        )
        embed.add_field(name='𝐒𝐄𝐓𝐔𝐏 𝐋𝐈𝐍𝐊',value=f'COPY THIS URL INTO YOUR BROWSER:\n{url}',inline=False)
        embed.add_field(name='𝐄𝐗𝐏𝐈𝐑𝐘',value='This signed one-time link expires in 10 minutes.',inline=False)
        await interaction.followup.send(embed=embed,ephemeral=True)

    class JobStreetView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)
        @discord.ui.button(label='CONNECT JOBSTREET',style=discord.ButtonStyle.primary,custom_id='jobhunter:jobstreet-connect')
        async def connect(self,interaction,button):
            await send_jobstreet_link(interaction,interaction.user.id)
        @discord.ui.button(label='CHECK CONNECTION',style=discord.ButtonStyle.secondary,custom_id='jobhunter:jobstreet-check')
        async def check(self,interaction,button):
            await interaction.response.defer(ephemeral=True,thinking=True)
            embed=await asyncio.to_thread(jobstreet_status_embed,interaction.user.id)
            await interaction.followup.send(embed=embed,view=JobStreetView(),ephemeral=True)
        @discord.ui.button(label='REAUTHENTICATE',style=discord.ButtonStyle.secondary,custom_id='jobhunter:jobstreet-reauth')
        async def reauthenticate(self,interaction,button):
            await send_jobstreet_link(interaction,interaction.user.id)
        @discord.ui.button(label='DISCONNECT',style=discord.ButtonStyle.danger,custom_id='jobhunter:jobstreet-disconnect')
        async def disconnect_source(self,interaction,button):
            from .jobstreet_link import disconnect
            await interaction.response.defer(ephemeral=True,thinking=True)
            removed=await asyncio.to_thread(disconnect,repo,interaction.user.id)
            message='The encrypted JobStreet session was removed.' if removed else 'No saved JobStreet session was found.'
            await interaction.followup.send(embed=styled_embed('𝐉𝐎𝐁𝐒𝐓𝐑𝐄𝐄𝐓 𝐃𝐈𝐒𝐂𝐎𝐍𝐍𝐄𝐂𝐓𝐄𝐃',message),ephemeral=True)
    class SearchModal(discord.ui.Modal,title='Find recent jobs'):
        role=discord.ui.TextInput(label='Job role / keyword',placeholder='Junior DevOps',max_length=100)
        location=discord.ui.TextInput(label='Location',default='Philippines',required=False,max_length=100)
        freshness=discord.ui.TextInput(label='Freshness',default='Past 24 hours',required=False,max_length=30)
        work_setup=discord.ui.TextInput(label='Work setup',placeholder='Remote / Hybrid / On-site',required=False,max_length=30)
        minimum_score=discord.ui.TextInput(label='Minimum match score',default='0',required=False,max_length=3)
        async def on_submit(self,interaction):
            await interaction.response.defer(ephemeral=True,thinking=True)
            channel=getattr(interaction,'channel',None) or getattr(interaction,'message',None)
            if not channel or not hasattr(channel,'send'):
                await interaction.followup.send('Please use `v!search <role>` in this channel to start a search.',ephemeral=True); return
            await execute_search(channel,interaction.user.id,str(self.role),str(self.location or 'Philippines'),str(self.freshness or 'Past 24 hours'),str(self.work_setup),str(self.minimum_score or '0'))
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
        @discord.ui.button(label='JOBSTREET',style=discord.ButtonStyle.secondary,custom_id='jobhunter:jobstreet')
        async def jobstreet(self,interaction,button):
            await interaction.response.defer(ephemeral=True,thinking=True)
            embed=await asyncio.to_thread(jobstreet_status_embed,interaction.user.id)
            await interaction.followup.send(embed=embed,view=JobStreetView(),ephemeral=True)
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
        @discord.ui.button(label='VIEW ALL JOBS',style=discord.ButtonStyle.secondary,custom_id='jobhunter:view-all')
        async def view_all(self,interaction,button):
            await interaction.response.defer(ephemeral=True,thinking=True)
            await send_view_all(interaction.followup,ephemeral=True)
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
            if not await asyncio.to_thread(repo.claim_bot_alert,job_id):
                continue
            def load_job():
                with repo.sessions() as s: return s.get(Job,job_id)
            job=await asyncio.to_thread(load_job)
            if not job: continue
            try:
                from .services import DISCORD_ALERT_ROLE_ALLOWLIST
                role_id=cfg.discord_alert_role_id if cfg.discord_alert_role_id in DISCORD_ALERT_ROLE_ALLOWLIST else None
                allowed=discord.AllowedMentions(everyone=False,users=False,roles=[discord.Object(id=int(role_id))] if role_id else [])
                headline=random.choice(cfg.discord_motivations)
                content=f'<@&{role_id}>\n\n{headline}' if role_id else headline
                embed,view=card(job); await channel.send(content=content,embed=embed,view=view,allowed_mentions=allowed)
                def mark_sent():
                    with repo.sessions() as s:
                        stored=s.get(Job,job_id)
                        if stored:
                            stored.notification_state='SENT'; stored.status=JobStatus.NOTIFIED.value; s.commit()
                await asyncio.to_thread(mark_sent)
            except Exception as exc:
                # Leave a failed claim retryable by the webhook fallback, but never
                # create a second successful bot alert for the same job.
                def mark_failed():
                    with repo.sessions() as s:
                        stored=s.get(Job,job_id)
                        if stored and stored.notification_state=='BOT_SENDING': stored.notification_state='FAILED'; s.commit()
                await asyncio.to_thread(mark_failed)
                log.warning('discord_bot_alert_failed',extra={'job_id':job_id,'error':str(exc)})
    async def panel_watcher():
        last=None
        while not bot.is_closed():
            state=await asyncio.to_thread(scheduler_snapshot); marker=(state.get('phase'),state.get('last_poll_at'),state.get('next_poll_at'))
            if marker!=last: await refresh_panel(); last=marker
            await deliver_pending_alerts()
            await asyncio.sleep(10)
    @bot.event
    async def on_ready():
        nonlocal panel_view,panel_task,registered,lease_task
        if not registered:
            panel_view=ControlView(); bot.add_view(panel_view); registered=True
        await refresh_panel()
        if not panel_task: panel_task=asyncio.create_task(panel_watcher())
        if not lease_task: lease_task=asyncio.create_task(renew_lease())
        await asyncio.to_thread(repo.set_state,'discord_bot_health',{'healthy':True,'started_once':True})
        log.info('discord_bot_ready')
    @bot.event
    async def on_message(message):
        if message.author.bot or not message.content.lower().startswith('v!'): return
        message_id=getattr(message,'id',None)
        now=time.monotonic()
        for seen_id,seen_at in list(processed_messages.items()):
            if now-seen_at>600: processed_messages.pop(seen_id,None)
        if message_id is not None:
            if message_id in processed_messages: return
            processed_messages[message_id]=now
        command,_,argument=message.content[2:].strip().partition(' '); command=command.lower(); argument=argument.strip()
        if command=='view' and argument.lower()=='all':
            command='viewall'; argument=''
        if command=='help':
            embed=styled_embed('𝐇𝐎𝐖 𝐓𝐎 𝐔𝐒𝐄','`v!search <role>`\n`v!latest`\n`v!viewall`\n`v!status`\n`v!scan`\n`v!jobstreet`\n`v!help`'); await message.channel.send(embed=embed); return
        if command=='jobstreet':
            await message.channel.send(
                embed=jobstreet_status_embed(message.author.id),
                view=JobStreetView(),
            )
            return
        if command=='resume':
            token=ActionTokens(cfg).issue_control('resume')
            if not token or not cfg.public_base_url:
                await message.channel.send('Resume upload is not configured. Set APP_SECRET_KEY and PUBLIC_BASE_URL first.'); return
            await message.channel.send(f'Upload or replace the saved resume here: {cfg.public_base_url.rstrip("/")}/resume/{token}'); return
        if command=='search':
            if not argument:
                await message.channel.send('Usage:\n`v!search <role>`\n\nExamples:\n`v!search Software Engineer`\n`v!search Junior DevOps`\n`v!search IT Support`'); return
            parts=[x.strip() for x in argument.split('|')]; role=parts[0]; location=parts[1] if len(parts)>1 and parts[1] else 'Philippines'; freshness='Past 24 hours' if len(parts)<3 or not parts[2] else ('Past 24 hours' if parts[2].lower() in ('24h','24 hours') else f'Past {parts[2]}')
            await execute_search(message.channel,message.author.id,role,location,freshness)
        elif command=='latest':
            from sqlalchemy import select
            from .models import Job, JobStatus
            def load_latest():
                with repo.sessions() as s:
                    return s.scalars(select(Job).where(Job.status!=JobStatus.EXPIRED.value,Job.score>=cfg.min_notify_score).order_by(Job.date_posted.desc()).limit(3)).all()
            jobs=await asyncio.to_thread(load_latest)
            for job in jobs:
                embed,view=card(job); await message.channel.send(embed=embed,view=view)
        elif command=='viewall':
            try:
                requested_page=max(1,int(argument.split()[0]))-1 if argument else 0
            except ValueError:
                requested_page=0
            await send_view_all(message.channel,page=requested_page)
        elif command=='status': await message.channel.send(embed=await panel_embed())
        elif command=='scan':
            if scan_task and not scan_task.done(): await message.channel.send(embed=styled_embed('𝐒𝐂𝐀𝐍𝐍𝐈𝐍𝐆','A scan is already running.')); return
            notice=await message.channel.send(embed=styled_embed('𝐒𝐂𝐀𝐍𝐍𝐈𝐍𝐆','Searching for recent active jobs…')); await launch_scan(notice)
        else: await message.channel.send('Use `v!help`.')
    try: await bot.start(cfg.discord_bot_token)
    finally:
        await asyncio.to_thread(repo.set_state,'discord_bot_health',{'healthy':False,'started_once':True})
        if panel_task: panel_task.cancel()
        if lease_task: lease_task.cancel()
        await asyncio.to_thread(repo.release_discord_bot_lease,instance_id)
        if not bot.is_closed(): await bot.close()
