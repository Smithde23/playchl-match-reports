"""PlayCHL match reports. Manual imports + optional verified game JSON feed polling."""
import asyncio
import csv
import io
import json
import logging
import os
import sqlite3
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands,tasks
from report import validate,three_stars,stat_line,render_card,summary
from playchl_site import fetch_playchl_games

logging.basicConfig(level=logging.INFO)
log=logging.getLogger('playchl.matchreports')
TOKEN=os.getenv('DISCORD_TOKEN','')
GUILD_ID=int(os.getenv('GUILD_ID','0'))
CHANNEL_ID=int(os.getenv('REPORT_CHANNEL_ID','0'))
FEED_URL=os.getenv('GAME_FEED_URL','').strip()
FEED_TOKEN=os.getenv('GAME_FEED_TOKEN','').strip()
PLAYCHL_PUBLIC=os.getenv('PLAYCHL_PUBLIC','1')=='1'
PLAYCHL_LOOKBACK=max(1,min(50,int(os.getenv('PLAYCHL_LOOKBACK','12'))))
DB_PATH=Path(os.getenv('DATABASE_PATH','/data/matchreports.sqlite3'))
POLL_MINUTES=max(2,int(os.getenv('POLL_MINUTES','5')))
MAX_FEED_GAMES=500

def db():
    DB_PATH.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(DB_PATH,timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, payload TEXT NOT NULL, posted_at TEXT, message_id TEXT, channel_id TEXT)')
    return conn

def store(game):
    validate(game)
    with db() as c:
        c.execute('INSERT INTO reports(id,payload) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (str(game['id']),json.dumps(game)))

def load(game_id):
    with db() as c:
        row=c.execute('SELECT payload,posted_at FROM reports WHERE id=?',(str(game_id),)).fetchone()
    return (json.loads(row[0]),row[1]) if row else (None,None)

def report_embed(game):
    em=discord.Embed(title='🏒 PLAYCHL • FINAL MATCH REPORT',description=f'**{summary(game)}**',color=0x2EA8DF)
    em.add_field(name='🥇 1st Star',value='Not available',inline=True)
    em.add_field(name='🥈 2nd Star',value='Not available',inline=True)
    em.add_field(name='🥉 3rd Star',value='Not available',inline=True)
    for i,p in enumerate(three_stars(game)):
        em.set_field_at(i,name=('🥇 1st Star','🥈 2nd Star','🥉 3rd Star')[i],value=f"**{p['name']}** ({p['team']})\n{stat_line(p)}",inline=True)
    em.add_field(name='Game ID',value=str(game['id']),inline=True)
    if game.get('source_url'):
        u=str(game['source_url'])
        if urlparse(u).scheme in ('https','http'):em.url=u
    em.set_footer(text='Three stars selected from the supplied game box score • Use /matchstats for full export')
    return em

intents=discord.Intents.default()
bot=commands.Bot(command_prefix='!',intents=intents)
post_lock=asyncio.Lock()

async def publish(game,force=False):
    validate(game)
    store(game)
    if not CHANNEL_ID:raise RuntimeError('REPORT_CHANNEL_ID is not set')
    async with post_lock:
        _,posted=load(game['id'])
        if posted and not force:return False
        ch=bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)
        picture=discord.File(io.BytesIO(render_card(game)),filename=f"match_{str(game['id']).replace('/','_')}.png")
        msg=await ch.send(embed=report_embed(game),file=picture)
        with db() as c:
            c.execute('UPDATE reports SET posted_at=?,message_id=?,channel_id=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),str(msg.id),str(ch.id),str(game['id'])))
        return True

async def fetch_feed():
    if not FEED_URL:
        return await fetch_playchl_games(limit=PLAYCHL_LOOKBACK) if PLAYCHL_PUBLIC else []
    if urlparse(FEED_URL).scheme != 'https':raise ValueError('Feed URL must use HTTPS')
    headers={'Authorization':f'Bearer {FEED_TOKEN}'} if FEED_TOKEN else {}
    timeout=aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(FEED_URL,headers=headers) as resp:
            resp.raise_for_status(); data=await resp.json(content_type=None)
    if isinstance(data,dict):data=data.get('games',[])
    if not isinstance(data,list):raise ValueError('Feed must return a list or {"games": [...]}')
    return data[:MAX_FEED_GAMES]

@tasks.loop(minutes=POLL_MINUTES)
async def poll_games():
    if not FEED_URL and not PLAYCHL_PUBLIC:return
    try:
        games=await fetch_feed()
        # First successful discovery creates a baseline; never dump old games into Discord.
        if PLAYCHL_PUBLIC and not FEED_URL and games:
            with db() as conn:
                initialized=conn.execute("SELECT 1 FROM reports WHERE id='__playchl_baseline__'").fetchone()
                if not initialized:
                    for g in games:
                        validate(g)
                        conn.execute('INSERT INTO reports(id,payload,posted_at) VALUES (?,?,?) ON CONFLICT(id) DO NOTHING',(str(g['id']),json.dumps(g),'baseline'))
                    conn.execute("INSERT INTO reports(id,payload,posted_at) VALUES (?,?,?)",('__playchl_baseline__','{}','baseline'))
                    log.info('Recorded %d historical PlayCHL games without posting',len(games))
                    return
        for g in games:
            try:
                if g.get('status','').lower().startswith('final'):
                    await publish(g)
            except Exception:log.exception('Unable to publish game %s',g.get('id'))
    except Exception:log.exception('Game feed fetch failed')

@poll_games.before_loop
async def before_poll():await bot.wait_until_ready()

@bot.event
async def on_ready():
    log.info('Match bot online as %s',bot.user)
    if not poll_games.is_running():poll_games.start()

@bot.event
async def setup_hook():
    db().close()
    if GUILD_ID:
        guild=discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:await bot.tree.sync()

@bot.tree.command(name='matchreport',description='Show a previously imported game report')
@app_commands.describe(game_id='Game identifier from the match feed')
async def matchreport(interaction:discord.Interaction,game_id:str):
    await interaction.response.defer(ephemeral=True)
    game,_=load(game_id)
    if not game:return await interaction.followup.send('Game not found. Import its box-score JSON first.',ephemeral=True)
    await interaction.followup.send(embed=report_embed(game),file=discord.File(io.BytesIO(render_card(game)),filename='report.png'),ephemeral=True)

@bot.tree.command(name='matchimport',description='Admin: import final game stats from a JSON attachment and publish')
@app_commands.describe(file='JSON file with game box stats',publish_now='Publish immediately to report channel')
@app_commands.checks.has_permissions(manage_guild=True)
async def matchimport(interaction:discord.Interaction,file:discord.Attachment,publish_now:bool=True):
    await interaction.response.defer(ephemeral=True)
    try:
        if file.size>2_000_000:raise ValueError('File exceeds 2MB')
        game=json.loads((await file.read()).decode('utf-8'))
        validate(game);store(game)
        published=await publish(game) if publish_now else False
        await interaction.followup.send(f"Imported game **{game['id']}**. "+('Posted to match-report channel.' if published else 'Saved (or already posted).'),ephemeral=True)
    except Exception as e:await interaction.followup.send(f'Import failed: {e}',ephemeral=True)

@bot.tree.command(name='matchfetch',description='Admin: retrieve one public PlayCHL game and publish its complete report')
@app_commands.checks.has_permissions(manage_guild=True)
async def matchfetch(interaction:discord.Interaction,game_id:str,publish_now:bool=True):
    await interaction.response.defer(ephemeral=True)
    import re
    if not re.fullmatch(r'[0-9a-fA-F]{24}',game_id):
        return await interaction.followup.send('Use the 24-character PlayCHL game ID from the game URL.',ephemeral=True)
    try:
        games=await fetch_playchl_games(include_id=game_id)
        if not games:return await interaction.followup.send('No complete final box score was extracted. Check Railway logs; nothing was posted.',ephemeral=True)
        game=games[0];store(game)
        posted=await publish(game) if publish_now else False
        await interaction.followup.send(('Posted.' if posted else 'Saved; already published or publish_now disabled.')+f' {summary(game)}',ephemeral=True)
    except Exception:
        log.exception('PlayCHL manual fetch failed')
        await interaction.followup.send('Page fetch failed; see Railway logs.',ephemeral=True)

@bot.tree.command(name='matchstats',description='Download the full available skater and goalie box stats for a game')
async def matchstats(interaction:discord.Interaction,game_id:str):
    game,_=load(game_id)
    if not game:return await interaction.response.send_message('Game ID not found.',ephemeral=True)
    players=game.get('players',[])
    if not players:return await interaction.response.send_message('No individual player box stats for this game.',ephemeral=True)
    headers=['team','name','position','goals','assists','points','plus_minus','shots','hits','penalty_minutes','giveaways','takeaways','faceoffs_won','faceoffs_taken','saves','shots_against','goals_against','save_pct']
    buf=io.StringIO();writer=csv.DictWriter(buf,fieldnames=headers,extrasaction='ignore');writer.writeheader();writer.writerows(players)
    await interaction.response.send_message(file=discord.File(io.BytesIO(buf.getvalue().encode('utf-8')),filename=f'boxstats_{game_id[:50]}.csv'),ephemeral=True)

@bot.tree.command(name='matchhistory',description='Show recent imported games')
async def matchhistory(interaction:discord.Interaction):
    with db() as c:rows=c.execute("SELECT payload,posted_at FROM reports WHERE id != '__playchl_baseline__' ORDER BY COALESCE(posted_at, '') DESC LIMIT 15").fetchall()
    lines=[f"`{json.loads(p)['id']}` • {summary(json.loads(p))} • {'Posted' if posted else 'Not posted'}" for p,posted in rows]
    await interaction.response.send_message('\n'.join(lines)[:1900] if lines else 'No match reports imported yet.',ephemeral=True)

@bot.tree.command(name='matchrepost',description='Admin: post a saved report again')
@app_commands.checks.has_permissions(manage_guild=True)
async def matchrepost(interaction:discord.Interaction,game_id:str):
    await interaction.response.defer(ephemeral=True)
    game,_=load(game_id)
    if not game:return await interaction.followup.send('Game not found.',ephemeral=True)
    await publish(game,force=True)
    await interaction.followup.send('Match report posted again.',ephemeral=True)

@bot.tree.command(name='matchrefresh',description='Admin: check configured game-feed endpoint immediately')
@app_commands.checks.has_permissions(manage_guild=True)
async def matchrefresh(interaction:discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not FEED_URL and not PLAYCHL_PUBLIC:return await interaction.followup.send('No public PlayCHL reader or GAME_FEED_URL enabled. Use /matchimport.',ephemeral=True)
    count=0;errors=0
    try:
        games=await fetch_feed()
        for g in games:
            try:
                if str(g.get('status','')).lower().startswith('final') and await publish(g):count+=1
            except Exception:errors+=1;log.exception('Game processing error')
        await interaction.followup.send(f'Checked {len(games)} games. New reports posted: {count}. Errors: {errors}.',ephemeral=True)
    except Exception as e:await interaction.followup.send(f'Feed error: {e}',ephemeral=True)

@bot.tree.error
async def command_error(interaction,error):
    message='Administrator permission required.' if isinstance(error,app_commands.MissingPermissions) else 'Command failed; check Railway logs.'
    if interaction.response.is_done():await interaction.followup.send(message,ephemeral=True)
    else:await interaction.response.send_message(message,ephemeral=True)

if __name__=='__main__':
    if not TOKEN:raise SystemExit('Set DISCORD_TOKEN in Railway environment variables')
    bot.run(TOKEN)
