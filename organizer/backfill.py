"""Administrator one-off historical attachment scan. Stop collector while running.

Uses separate progress; never resets live recovery cursors or sends Discord messages.
The existing collector saves files/deduplicates; its Drive queue resumes on restart.
"""
import asyncio
from contextlib import closing
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sqlite3

import discord
from dotenv import load_dotenv
from bot import discord_bot as collector


async def main():
    root=Path(__file__).resolve().parent.parent
    load_dotenv(root/'.env')
    collector.CHANNEL_MAP=collector.load_channel_map()
    collector.FORUM_MAP=collector.load_channel_map('forums')
    progress=Path.home()/'.local/state/project-hub-ai/backfill.db'
    progress.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(progress,os.O_CREAT|os.O_RDWR,0o600)
    os.close(fd)
    with closing(sqlite3.connect(progress)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS cursors(channel TEXT PRIMARY KEY,cursor INTEGER)')
    cutoff=discord.utils.time_snowflake(datetime.now(timezone.utc))
    totals={'messages':0,'attachments_seen':0,'channels':0,'failures':[]}
    seen=set()

    async def scan(channel,category):
        if channel.id in seen: return
        seen.add(channel.id)
        channel_id=str(channel.id)
        # Explicitly include threads belonging to an authorized text channel.
        collector.CHANNEL_MAP[channel_id]=category
        with closing(sqlite3.connect(progress)) as db:
            row=db.execute('SELECT cursor FROM cursors WHERE channel=?',(channel_id,)).fetchone()
        cursor=row[0] if row else 0
        count=attachments=0
        try:
            async for message in channel.history(limit=None,oldest_first=True,
                    after=discord.Object(id=cursor),before=discord.Object(id=cutoff)):
                if not await asyncio.wait_for(collector.process_message(message,upload=False),timeout=120):
                    raise RuntimeError('AttachmentSaveFailed')
                with closing(sqlite3.connect(progress)) as db, db:
                    db.execute('INSERT OR REPLACE INTO cursors VALUES(?,?)',(channel_id,message.id))
                count+=1
                attachments+=len(message.attachments) if not message.author.bot else 0
            totals['channels']+=1
        except Exception as e:
            totals['failures'].append({'channel':channel_id,'error':type(e).__name__})
        totals['messages']+=count
        totals['attachments_seen']+=attachments
        print(json.dumps({'channel':channel_id,'messages':count,'attachments_seen':attachments}),flush=True)

    async with discord.Client(intents=discord.Intents.none()) as client:
        await client.login(os.environ['DISCORD_BOT_TOKEN'])
        guilds={}
        parents={**collector.CHANNEL_MAP,**collector.FORUM_MAP}
        for channel_id,category in list(parents.items()):
            try:
                channel=await client.fetch_channel(int(channel_id))
                guilds[channel.guild.id]=channel.guild
                if isinstance(channel,discord.TextChannel):
                    await scan(channel,category)
                if isinstance(channel,(discord.TextChannel,discord.ForumChannel)):
                    async for thread in channel.archived_threads(limit=None):
                        await scan(thread,category)
            except Exception as e:
                totals['failures'].append({'channel':channel_id,'error':type(e).__name__})
        for guild in guilds.values():
            try:
                for thread in await guild.active_threads():
                    category=parents.get(str(thread.parent_id))
                    if category: await scan(thread,category)
            except Exception as e:
                totals['failures'].append({'guild':str(guild.id),'error':type(e).__name__})
    print(json.dumps(totals),flush=True)
    if totals['failures']: raise SystemExit(1)


if __name__=='__main__':
    asyncio.run(main())
