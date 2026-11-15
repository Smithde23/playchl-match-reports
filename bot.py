"""PlayCHL match reports. Manual imports + optional verified game JSON feed polling."""
import asyncio
import csv
import io
import json
import logging
import os
import re
import sqlite3
import zipfile
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands,tasks
from report import validate,three_stars,stat_line,render_card,summary,num,label
from playchl_site import fetch_playchl_games

logging.basicConfig(level=logging.INFO)
log=logging.getLogger('playchl.matchreports')
TOKEN=os.getenv('DISCORD_TOKEN','')
GUILD_ID=int(os.getenv('GUILD_ID','0'))
CHANNEL_ID=int(os.getenv('REPORT_CHANNEL_ID','0'))
FEED_URL=os.getenv('GAME_FEED_URL','').strip()
FEED_TOKEN=os.getenv('GAME_FEED_TOKEN','').strip()
PLAYCHL_PUBLIC=os.getenv('PLAYCHL_PUBLIC','1')=='1'
PLAYCHL_LOOKBACK=max(1,min(100,int(os.getenv('PLAYCHL_LOOKBACK','10'))))
TARGET_TEAM_NAME=os.getenv('TARGET_TEAM_NAME','Washington Capitals').strip() or 'Washington Capitals'
DB_PATH=Path(os.getenv('DATABASE_PATH','/data/matchreports.sqlite3'))
POLL_MINUTES=max(2,int(os.getenv('POLL_MINUTES','5')))
MAX_FEED_GAMES=500

def is_target_game(game):
    target=re.sub(r'[^a-z0-9]','',TARGET_TEAM_NAME.casefold())
    for side in ('home','away'):
        name=re.sub(r'[^a-z0-9]','',str(game.get(side,{}).get('name','')).casefold())
        if name and (target in name or name in ('wsh','capitals','washingtoncapitals')):
            return True
    return False

def game_date_key(game):
    value=str(game.get('date') or '')
    for fmt in ('%b %d, %Y, %I:%M %p','%b %d, %Y','%m/%d/%Y %I:%M %p','%m/%d/%Y','%Y-%m-%d'):
        try:
            parsed=datetime.strptime(value,fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)

def db():
    DB_PATH.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(DB_PATH,timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, payload TEXT NOT NULL, posted_at TEXT, message_id TEXT, channel_id TEXT)')
    return conn

def store(game):
    validate(game)
    if not is_target_game(game):
        raise ValueError(f'This bot only stores {TARGET_TEAM_NAME} games.')
    with db() as c:
        c.execute('INSERT INTO reports(id,payload) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (str(game['id']),json.dumps(game)))

def load(game_id):
    with db() as c:
        row=c.execute('SELECT payload,posted_at FROM reports WHERE id=?',(str(game_id),)).fetchone()
    return (json.loads(row[0]),row[1]) if row else (None,None)

def build_boxscore_csv(game):
    """Return every available player-stat field as UTF-8 CSV bytes."""
    players=game.get('players') or []
    if not players:return None
    preferred=['team','name','position','goals','assists','points','plus_minus','shots',
               'hits','time_on_ice','time_with_puck','passing_pct','faceoffs_won',
               'faceoffs_taken','penalty_minutes','giveaways','takeaways','blocks',
               'interceptions','saves','shots_against','goals_against','save_pct','gaa',
               'player_score','breakaway_pct','breakaways','diving_saves',
               'penalty_shot_pct','poke_checks']
    present=set().union(*(p.keys() for p in players))
    headers=[k for k in preferred if k in present]+sorted(present-set(preferred))
    buf=io.StringIO()
    writer=csv.DictWriter(buf,fieldnames=headers,extrasaction='ignore')
    writer.writeheader()
    writer.writerows(players)
    return buf.getvalue().encode('utf-8')

def build_boxscore_zip(game):
    """Package the full CSV as a ZIP so Discord doesn't expand it as a text preview."""
    csv_bytes=build_boxscore_csv(game)
    if not csv_bytes:return None
    safe_id=re.sub(r'[^A-Za-z0-9_-]','_',str(game['id']))[:70]
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"boxstats_{safe_id}.csv",csv_bytes)
    return buffer.getvalue()

def report_embed(game):
    away,home=game['away'],game['home']
    em=discord.Embed(title='🏒 PLAYCHL • OFFICIAL MATCH REPORT',
        description=f"**{away['name']} {away['score']} — {home['name']} {home['score']} (FINAL)**",
        color=0x2EA8DF)
    capitals_side=next((side for side in ('away','home')
        if 'washington' in game[side]['name'].casefold() or 'capitals' in game[side]['name'].casefold()),None)
    if capitals_side:
        other='home' if capitals_side=='away' else 'away'
        result='WIN' if game[capitals_side]['score']>game[other]['score'] else 'LOSS' if game[capitals_side]['score']<game[other]['score'] else 'TIE'
        em.add_field(name='Result',value=f"**{result}** · {game.get('date','Date unavailable')}",inline=False)
    elif game.get('date'):
        em.add_field(name='Game date',value=str(game['date']),inline=False)
    stars=three_stars(game)
    if stars:
        labels=('🥇 1st Star','🥈 2nd Star','🥉 3rd Star')
        for i,p in enumerate(stars[:3]):
            em.add_field(name=labels[i],value=f"**{p['name']}** ({p.get('team','')})\n{stat_line(p)}",inline=True)
    team_stats=game.get('team_stats') or {}
    stat_labels=[('Shots','shots'),('Hits','hits'),('Faceoffs %','faceoff_pct'),
        ('Power-play goals','power_play_goals'),('Penalty minutes','penalty_minutes'),
        ('Blocks','blocks'),('Takeaways','takeaways'),('Giveaways','giveaways'),
        ('Interceptions','interceptions'),('Time on attack','time_on_attack')]
    for side in ('away','home'):
        values=team_stats.get(side,{})
        lines=[f"**{label}:** {values[key]}" for label,key in stat_labels if values.get(key) is not None]
        if lines:
            em.add_field(name=f"📊 {game[side]['name']} — Team Stats",value='\n'.join(lines)[:1024],inline=True)
    players=game.get('players') or []
    for side in ('away','home'):
        team=game[side]['name']
        roster=[p for p in players if p.get('team')==team]
        skaters=[p for p in roster if str(p.get('position','')).upper() not in ('G','GOL','GOALIE')]
        goalies=[p for p in roster if str(p.get('position','')).upper() in ('G','GOL','GOALIE')]
        if skaters:
            lines=[]
            for p in skaters:
                pts=p.get('points',num(p.get('goals'))+num(p.get('assists')))
                lines.append(f"**{p['name']}** · G {label(p.get('goals'))} · A {label(p.get('assists'))} · P {label(pts)} · S {label(p.get('shots'))} · H {label(p.get('hits'))}")
            for chunk_no in range(0,len(lines),8):
                em.add_field(name=f"🏒 {team} — Player Statistics"+(f" ({chunk_no//8+1})" if len(lines)>8 else ""),
                    value='\n'.join(lines[chunk_no:chunk_no+8])[:1024],inline=False)
        if goalies:
            goalie_lines=[f"**{p['name']}** · Saves: {label(p.get('saves'))} · SA: {label(p.get('shots_against'))} · GA: {label(p.get('goals_against'))} · SV%: {label(p.get('save_pct'))}"
                for p in goalies]
            em.add_field(name=f"🥅 {team} — Goalie Statistics",value='\n'.join(goalie_lines)[:1024],inline=False)
    em.add_field(name='Game ID',value=str(game['id']),inline=False)
    if game.get('source_url'):
        u=str(game['source_url'])
        if urlparse(u).scheme in ('https','http'):em.url=u
    em.set_footer(text='Official PlayCHL match report.')
    return em


intents=discord.Intents.default()
bot=commands.Bot(command_prefix='!',intents=intents)
post_lock=asyncio.Lock()

def report_quality(game):
    """Higher score means a richer extraction for the same completed game."""
    players=len(game.get('players') or [])
    stars=len(three_stars(game))
    team_stats=sum(len(v or {}) for v in (game.get('team_stats') or {}).values())
    return players*10+stars*100+team_stats

async def publish(game,force=False):
    validate(game)
    if not is_target_game(game):
        raise ValueError(f'This bot only publishes {TARGET_TEAM_NAME} games.')
    if not CHANNEL_ID:raise RuntimeError('REPORT_CHANNEL_ID is not set')
    async with post_lock:
        with db() as conn:
            row=conn.execute(
                'SELECT payload,posted_at,message_id,channel_id FROM reports WHERE id=?',
                (str(game['id']),)
            ).fetchone()
        old_game=json.loads(row[0]) if row else None
        posted=row[1] if row else None
        message_id=row[2] if row else None
        saved_channel_id=row[3] if row else None
        old_quality=report_quality(old_game) if old_game else -1
        new_quality=report_quality(game)
        store(game)

        # Existing reports are edited in place when a later scrape finds richer stats.
        # Historical baseline entries are saved only; they must never be posted retroactively.
        if posted and not force:
            if posted=='baseline' or not message_id:
                return False
            if new_quality<=old_quality:
                return False
            try:
                ch=bot.get_channel(int(saved_channel_id or CHANNEL_ID)) or await bot.fetch_channel(int(saved_channel_id or CHANNEL_ID))
                msg=await ch.fetch_message(int(message_id))
                safe_id=re.sub(r'[^A-Za-z0-9_-]','_',str(game['id']))[:70]
                graphic=discord.File(io.BytesIO(render_card(game)),filename=f"match_{safe_id}.png")
                await msg.edit(embed=report_embed(game),attachments=[graphic])
                log.info("Enriched existing match report %s: quality %s -> %s",game['id'],old_quality,new_quality)
            except Exception:
                log.exception("Could not update existing Discord report for enriched game %s",game['id'])
            return False

        ch=bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)
        safe_id=re.sub(r'[^A-Za-z0-9_-]','_',str(game['id']))[:70]
        files=[discord.File(io.BytesIO(render_card(game)),filename=f"match_{safe_id}.png")]
        msg=await ch.send(embed=report_embed(game),files=files)
        with db() as conn:
            conn.execute(
                'UPDATE reports SET posted_at=?,message_id=?,channel_id=? WHERE id=?',
                (datetime.now(timezone.utc).isoformat(),str(msg.id),str(ch.id),str(game['id']))
            )
        if not game.get('boxscore_complete'):
            log.warning("Published scorecard %s with partial stats; later polls will retry and enrich it",game['id'])
        return True

async def fetch_feed():
    if not FEED_URL:
        games=await fetch_playchl_games(limit=PLAYCHL_LOOKBACK,team_name=TARGET_TEAM_NAME) if PLAYCHL_PUBLIC else []
        return [g for g in games if is_target_game(g)]
    if urlparse(FEED_URL).scheme != 'https':raise ValueError('Feed URL must use HTTPS')
    headers={'Authorization':f'Bearer {FEED_TOKEN}'} if FEED_TOKEN else {}
    timeout=aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(FEED_URL,headers=headers) as resp:
            resp.raise_for_status(); data=await resp.json(content_type=None)
    if isinstance(data,dict):data=data.get('games',[])
    if not isinstance(data,list):raise ValueError('Feed must return a list or {"games": [...]}')
    return [g for g in data[:MAX_FEED_GAMES] if is_target_game(g)]

@tasks.loop(minutes=POLL_MINUTES)
async def poll_games():
    if not FEED_URL and not PLAYCHL_PUBLIC:return
    try:
        games=await fetch_feed()
        log.info('Automatic poll found %d complete %s games',len(games),TARGET_TEAM_NAME)
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
    if not is_target_game(game):return await interaction.followup.send(f'This bot is configured for {TARGET_TEAM_NAME} games only.',ephemeral=True)
    safe_id=re.sub(r'[^A-Za-z0-9_-]','_',str(game['id']))[:70]
    files=[discord.File(io.BytesIO(render_card(game)),filename=f"match_{safe_id}.png")]
    await interaction.followup.send(embed=report_embed(game),files=files,ephemeral=True)

@bot.tree.command(name='latestreport',description='Show the most recently saved Washington Capitals match report without a game ID')
async def latestreport(interaction:discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    with db() as conn:
        rows=conn.execute(
            "SELECT payload FROM reports WHERE id != '__playchl_baseline__'"
        ).fetchall()
    saved=[json.loads(payload) for (payload,) in rows]
    saved=[candidate for candidate in saved if is_target_game(candidate)]
    saved.sort(key=game_date_key,reverse=True)
    game=saved[0] if saved else None
    if not game:
        return await interaction.followup.send(
            f"No saved {TARGET_TEAM_NAME} match report was found yet. The next completed game will appear here after it is imported.",
            ephemeral=True
        )
    safe_id=re.sub(r'[^A-Za-z0-9_-]','_',str(game['id']))[:70]
    files=[discord.File(io.BytesIO(render_card(game)),filename=f"match_{safe_id}.png")]
    await interaction.followup.send(
        content="**Most recently saved Washington Capitals match report**",
        embed=report_embed(game),files=files,ephemeral=True
    )

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

@bot.tree.command(name='matchfetch',description='Admin: retrieve a Washington Capitals game and publish its complete report')
@app_commands.checks.has_permissions(manage_guild=True)
async def matchfetch(interaction:discord.Interaction,game_id:str,publish_now:bool=True):
    await interaction.response.defer(ephemeral=True)
    import re
    if not re.fullmatch(r'[0-9a-fA-F]{24}',game_id):
        return await interaction.followup.send('Use the 24-character PlayCHL game ID from the game URL.',ephemeral=True)
    try:
        games=await fetch_playchl_games(include_id=game_id,team_name=TARGET_TEAM_NAME)
        if not games:return await interaction.followup.send(f'No complete {TARGET_TEAM_NAME} box score was extracted for that ID. Non-Capitals games are ignored; nothing was posted.',ephemeral=True)
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
    zip_bytes=build_boxscore_zip(game)
    if not zip_bytes:return await interaction.response.send_message('No individual player box stats for this game.',ephemeral=True)
    await interaction.response.send_message(file=discord.File(io.BytesIO(zip_bytes),filename=f'boxstats_{game_id[:50]}.zip'),ephemeral=True)

@bot.tree.command(name='matchhistory',description='Show recent imported games')
async def matchhistory(interaction:discord.Interaction):
    with db() as c:rows=c.execute("SELECT payload,posted_at FROM reports WHERE id != '__playchl_baseline__'").fetchall()
    saved=[(json.loads(payload),posted) for payload,posted in rows]
    saved=[item for item in saved if is_target_game(item[0])]
    saved.sort(key=lambda item:game_date_key(item[0]),reverse=True)
    lines=[f"`{game['id']}` • {game.get('date','Date unavailable')} • {summary(game)} • {'Posted' if posted and posted != 'baseline' else 'Saved'}" for game,posted in saved[:10]]
    await interaction.response.send_message('\n'.join(lines)[:1900] if lines else 'No match reports imported yet.',ephemeral=True)

@bot.tree.command(name='matchrepost',description='Admin: post a saved report again')
@app_commands.checks.has_permissions(manage_guild=True)
async def matchrepost(interaction:discord.Interaction,game_id:str):
    await interaction.response.defer(ephemeral=True)
    game,_=load(game_id)
    if not game:return await interaction.followup.send('Game not found.',ephemeral=True)
    if not is_target_game(game):return await interaction.followup.send(f'This bot is configured for {TARGET_TEAM_NAME} games only.',ephemeral=True)
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
