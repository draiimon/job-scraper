"""Development-only Discord UI gallery.

Run explicitly with ``python -m src.preview_discord_ui``.  It uses the same
production embed factory as the bot, never opens the gateway, writes no
database state, and disables every type of Discord mention.
"""
from __future__ import annotations

import asyncio
import os
import argparse
from types import SimpleNamespace

import httpx
from dotenv import load_dotenv

from .applications import cover_letter, discord_cover_letter_chunks
from .discord_bot import MASTER_EMBED_COLOUR, MASTER_EMBED_FOOTER, make_after_hours_embed


def _job():
    return SimpleNamespace(
        id=0, title="Software Engineer", company="Example Technologies PH",
        location="Makati City, Philippines · Hybrid", description="Python JavaScript Node.js PostgreSQL Docker entry-level",
        url="https://example.invalid/job", application_url="https://example.invalid/apply",
        application_email=None, score=82, match_reasons=["Relevant entry-level technology role", "Python", "Docker", "Entry-level indicator"],
        warnings=[], raw_metadata={}, work_setup="Hybrid",
    )


def gallery_embeds(footer_icon_url: str | None = None):
    """Return every meaningful layout using the production shared renderer."""
    job=_job()
    resume="""Mark Andrei R. Castillo
Bacoor City, Cavite, Philippines
+63 953 852 1829
andreicastillofficial@gmail.com
https://github.com/draiimon
https://www.draiimon.gt.tc/

Cloud DevOps Intern — Oaktree Innovations
Worked with AWS, Docker, Terraform, Linux, and GitHub Actions.
"""
    letter=cover_letter(job,resume)
    states=[]
    def add(group,title,description,fields=()):
        embed=make_after_hours_embed(title,description,footer_icon_url=footer_icon_url)
        for name,value,inline in fields:
            embed.add_field(name=name,value=value,inline=inline)
        states.append((group,embed))
    add("SYSTEM UI","𝐀𝐅𝐓𝐄𝐑 𝐇𝐎𝐔𝐑𝐒 𝐉𝐎𝐁 𝐇𝐔𝐍𝐓𝐄𝐑","Your automated Philippine tech-job monitor is online.",(
        ("𝐒𝐘𝐒𝐓𝐄𝐌 𝐒𝐓𝐀𝐓𝐔𝐒","Service\nONLINE\n\nScheduler\nRUNNING\n\nLast scan\n4 minutes ago\n\nNext scan\nin 11 minutes",True),
        ("𝐂𝐎𝐍𝐍𝐄𝐂𝐓𝐈𝐎𝐍𝐒","ATS Sources\n6 READY\n\nLinkedIn\nDISABLED\n\nJobStreet\nAUTH REQUIRED\n\nDatabase\nCONNECTED",True),
    ))
    add("SYSTEM UI","𝐒𝐂𝐀𝐍𝐍𝐈𝐍𝐆","Looking for recent, active jobs.",(("𝐏𝐑𝐎𝐆𝐑𝐄𝐒𝐒","Sources checked: 4 / 6\nJobs reviewed: 38\nRecent jobs: 9\nPotential matches: 3",False),))
    add("SYSTEM UI","𝐒𝐂𝐀𝐍 𝐂𝐎𝐌𝐏𝐋𝐄𝐓𝐄","The scheduled scan finished successfully.",(("𝐑𝐄𝐒𝐔𝐋𝐓𝐒","Sources checked: 6\nJobs reviewed: 86\nQualifying jobs: 3\nAlerts sent: 2",False),))
    add("SEARCH UI","𝐒𝐄𝐀𝐑𝐂𝐇𝐈𝐍𝐆","**Software Engineer**\nPhilippines · Recent jobs\n\nStarting targeted discovery…")
    add("SEARCH UI","𝐒𝐄𝐀𝐑𝐂𝐇 𝐂𝐎𝐌𝐏𝐋𝐄𝐓𝐄","**Software Engineer**\n\nSources checked: 6 / 6\nJobs reviewed: 48\nRecent PH tech jobs: 10\nMatching jobs: 2\nDuration: 4.2s")
    add("SEARCH UI","𝐍𝐎 𝐑𝐄𝐂𝐄𝐍𝐓 𝐌𝐀𝐓𝐂𝐇𝐄𝐒","**Software Engineer**\n\nSources checked: 6 / 6\nJobs reviewed: 48\nRecent PH tech jobs: 7\nQualifying matches: 0\n\nTry: Software Developer · Junior Developer · Backend Developer")
    add("SEARCH UI","𝐒𝐄𝐀𝐑𝐂𝐇 𝐂𝐎𝐎𝐋𝐃𝐎𝐖𝐍","Please wait 18 seconds before searching again.")
    add("JOB UI","𝐇𝐈𝐆𝐇 𝐌𝐀𝐓𝐂𝐇","**82% MATCH**\n\n**Software Engineer**\nExample Technologies PH\nMakati City · Hybrid",(("𝐖𝐇𝐘 𝐈𝐓 𝐅𝐈𝐓𝐒","Python\nDocker\nEntry-level compatible",False),))
    add("JOB UI","𝐍𝐄𝐖 𝐉𝐎𝐁 𝐀𝐋𝐄𝐑𝐓","@Job Alerts · Preview only\n\n**Software Engineer**\nExample Technologies PH\nPosted 4 hours ago\n\nNo role is pinged in this gallery.")
    add("APPLICATION UI","𝐀𝐏𝐏𝐋𝐈𝐂𝐀𝐓𝐈𝐎𝐍 𝐑𝐄𝐕𝐈𝐄𝐖","**Software Engineer**\nExample Technologies PH",(
        ("𝐌𝐀𝐓𝐂𝐇","82% MATCH",True),("𝐑𝐄𝐒𝐔𝐌𝐄","READY",True),("𝐂𝐎𝐕𝐄𝐑 𝐋𝐄𝐓𝐓𝐄𝐑","READY (TEMPLATE)",True),("𝐀𝐏𝐏𝐋𝐈𝐂𝐀𝐓𝐈𝐎𝐍 𝐌𝐄𝐓𝐇𝐎𝐃","PORTAL",True),("𝐒𝐀𝐅𝐄𝐓𝐘","DRY RUN ON",False),
    ))
    for index,chunk in enumerate(discord_cover_letter_chunks(letter),start=1):
        add("APPLICATION UI",f"𝐂𝐎𝐕𝐄𝐑 𝐋𝐄𝐓𝐓𝐄𝐑 ({index})",chunk)
    add("APPLICATION UI","𝐂𝐎𝐍𝐅𝐈𝐑𝐌 𝐀𝐏𝐏𝐋𝐈𝐂𝐀𝐓𝐈𝐎𝐍","**Software Engineer**\nExample Technologies PH",(("𝐃𝐎𝐂𝐔𝐌𝐄𝐍𝐓𝐒","Resume\nREADY\n\nCover Letter\nREADY",True),("𝐒𝐀𝐅𝐄𝐓𝐘","Dry Run\nON\n\nNo employer will be contacted.",True)))
    add("APPLICATION UI","𝐃𝐑𝐘 𝐑𝐔𝐍 𝐂𝐎𝐌𝐏𝐋𝐄𝐓𝐄","**Software Engineer**\nExample Technologies PH\n\nYour application package was prepared successfully.",(("𝐄𝐌𝐏𝐋𝐎𝐘𝐄𝐑 𝐂𝐎𝐍𝐓𝐀𝐂𝐓𝐄𝐃","NO",True),("𝐒𝐀𝐅𝐄𝐓𝐘","This was a simulation only. No application was sent.",True)))
    add("ERROR / SOURCES","𝐒𝐎𝐔𝐑𝐂𝐄 𝐒𝐓𝐀𝐓𝐔𝐒","LinkedIn is disabled; ATS sources continue to run normally.\n\nJobStreet requires an authenticated session before it can be used.")
    add("ERROR / SOURCES","𝐖𝐀𝐑𝐍𝐈𝐍𝐆","One source is temporarily unavailable. The remaining sources are still being checked.")
    add("ERROR / SOURCES","𝐀𝐂𝐓𝐈𝐎𝐍 𝐔𝐍𝐀𝐕𝐀𝐈𝐋𝐀𝐁𝐋𝐄","We could not complete that action. Please try again in a moment.")
    return states


def job_board_30_preview(footer_icon_url: str | None = None):
    """One safe visual-only page of the eventual 30-result Discord board."""
    rows=[]
    roles=["Software Engineer","Junior Cloud Engineer","IT Support Specialist","Backend Developer","QA Engineer"]
    for index,role in enumerate(roles,start=1):
        rows.append(f'`{index:02}  Sep {15-index:02}  ·  {84-index}% MATCH`\n**{role}**\nExample Technologies PH · Makati City · Hybrid')
    embed=make_after_hours_embed('𝐉𝐎𝐁 𝐁𝐎𝐀𝐑𝐃','Recent stored matches · newest posted first\n\n'+'\n\n'.join(rows))
    if footer_icon_url:
        embed.set_footer(text=MASTER_EMBED_FOOTER,icon_url=footer_icon_url)
    embed.add_field(name='𝐍𝐀𝐕𝐈𝐆𝐀𝐓𝐈𝐎𝐍',value='Page 1 of 6 · 30 stored matches\n\nButton preview: PREVIOUS · PAGE 1 / 6 · NEXT',inline=False)
    return embed


async def publish(channel_id: str, token: str, start_at: int = 0) -> int:
    count=0
    async with httpx.AsyncClient(timeout=20) as client:
        me=await client.get('https://discord.com/api/v10/users/@me',headers={"Authorization":f"Bot {token}"})
        me.raise_for_status(); identity=me.json()
        avatar=identity.get('avatar')
        footer_icon_url=f"https://cdn.discordapp.com/avatars/{identity['id']}/{avatar}.png?size=128" if avatar else None
        for group,embed in gallery_embeds(footer_icon_url)[start_at:]:
            payload={"content":f"── {group} · DEVELOPMENT PREVIEW ──", "embeds":[embed.to_dict()], "allowed_mentions":{"parse":[]}}
            response=await client.post(f"https://discord.com/api/v10/channels/{channel_id}/messages",headers={"Authorization":f"Bot {token}"},json=payload)
            if response.status_code == 429:
                await asyncio.sleep(float(response.headers.get("Retry-After", "1")) + .25)
                response=await client.post(f"https://discord.com/api/v10/channels/{channel_id}/messages",headers={"Authorization":f"Bot {token}"},json=payload)
            response.raise_for_status(); count+=1
    return count


def main():
    parser=argparse.ArgumentParser(description="Publish the development-only Discord UI gallery.")
    parser.add_argument('--start-at',type=int,default=0,help='Resume from a zero-based gallery item after a rate-limited run.')
    parser.add_argument('--job-board-30',action='store_true',help='Publish one visual-only 30-result board preview.')
    args=parser.parse_args()
    load_dotenv()
    token=os.getenv("DISCORD_BOT_TOKEN")
    channel_id=os.getenv("DISCORD_CONTROL_CHANNEL_ID")
    if not token or not channel_id:
        raise SystemExit("DISCORD_BOT_TOKEN and DISCORD_CONTROL_CHANNEL_ID are required for the preview gallery.")
    if args.job_board_30:
        async def send_board():
            async with httpx.AsyncClient(timeout=20) as client:
                me=await client.get('https://discord.com/api/v10/users/@me',headers={"Authorization":f"Bot {token}"}); me.raise_for_status(); identity=me.json()
                avatar=identity.get('avatar'); icon=f"https://cdn.discordapp.com/avatars/{identity['id']}/{avatar}.png?size=128" if avatar else None
                response=await client.post(f"https://discord.com/api/v10/channels/{channel_id}/messages",headers={"Authorization":f"Bot {token}"},json={"content":"𝐉𝐎𝐁 𝐁𝐎𝐀𝐑𝐃 · DEVELOPMENT VISUAL PREVIEW\nNo jobs, applications, or role pings are created by this message.","embeds":[job_board_30_preview(icon).to_dict()],"allowed_mentions":{"parse":[]}})
                response.raise_for_status()
        asyncio.run(send_board()); print('Published the 30-result job-board visual preview.')
        return
    count=asyncio.run(publish(channel_id,token,args.start_at))
    print(f"Published {count} development-only Discord UI previews.")


if __name__ == "__main__":
    main()
