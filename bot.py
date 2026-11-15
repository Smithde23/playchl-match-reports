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
POLL_MINUTES=max(2,int(os.getenv('POLL_MINUTES','15')))
# Auto-post only genuinely new games; historical games remain selectable in /games.
AUTO_POST_SINCE=os.getenv('AUTO_POST_SINCE','2026-11-15').strip()
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
        # When the admin has supplied explicit game IDs, both manual refresh
        # and the scheduled 15-minute poll must stay inside that exact set.
        # Do not mix in the general schedule's newest-ten games.
        extra_ids=[x.strip() for x in os.getenv('PLAYCHL_EXTRA_GAME_IDS','').split(',') if x.strip()]
        if extra_ids:
            games=[]
            seen=set()
            for game_id in extra_ids:
                if game_id in seen:continue
                seen.add(game_id)
                try:
                    recovered=await fetch_playchl_games(limit=1,include_id=game_id,team_name=TARGET_TEAM_NAME)
                    games.extend(g for g in recovered if is_target_game(g))
                except Exception:
                    log.exception('Unable to fetch selected PlayCHL game %s',game_id)
            games.sort(key=game_date_key)
            log.info('Selected-game mode: fetched %d of %d configured %s game IDs; no schedule games will be added',
                     len(games),len(seen),TARGET_TEAM_NAME)
            for game in games:
                log.info('Selected match: id=%s date=%s result=%s %s-%s %s',
                         game.get('id'),game.get('date','Date unavailable'),
                         game.get('away',{}).get('name'),game.get('away',{}).get('score'),
                         game.get('home',{}).get('score'),game.get('home',{}).get('name'))
            return games
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
    games=[g for g in data[:MAX_FEED_GAMES] if is_target_game(g)]
    games.sort(key=game_date_key)
    return games

def auto_post_cutoff():
    try:
        return datetime.strptime(AUTO_POST_SINCE,'%Y-%m-%d').replace(tzinfo=timezone.utc)
    except ValueError:
        log.error("Invalid AUTO_POST_SINCE %r: expected YYYY-MM-DD; disabling automatic historical posting",AUTO_POST_SINCE)
        return datetime.max.replace(tzinfo=timezone.utc)

async def fetch_recent_games():
    """Scheduled polling always uses the recent PlayCHL schedule, not manual backfill IDs."""
    if not FEED_URL:
        games=await fetch_playchl_games(limit=PLAYCHL_LOOKBACK,team_name=TARGET_TEAM_NAME) if PLAYCHL_PUBLIC else []
        games=[game for game in games if is_target_game(game)]
    else:
        games=await fetch_feed()
    games.sort(key=lambda g:(game_date_key(g),str(g.get('id'))))
    return games

@tasks.loop(minutes=POLL_MINUTES)
async def poll_games():
    if not FEED_URL and not PLAYCHL_PUBLIC:return
    try:
        games=await fetch_recent_games()
        log.info('Automatic schedule poll found %d complete %s games',len(games),TARGET_TEAM_NAME)
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
        cutoff=auto_post_cutoff()
        for g in games:
            try:
                if not str(g.get('status','')).lower().startswith('final'):
                    continue
                # Keep historical games searchable without flooding the channel.
                if game_date_key(g)<cutoff:
                    if not load(g['id'])[0]:store(g)
                    continue
                if not g.get('date'):
                    log.warning('Auto-post skipped undated game %s (available to select if saved)',g.get('id'))
                    store(g)
                    continue
                await publish(g)
            except Exception:
                log.exception('Unable to auto-publish game %s',g.get('id'))
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

def list_saved_games():
    """All stored Capitals games, newest first; the menu pages through every entry."""
    with db() as conn:
        records=conn.execute("SELECT payload FROM reports WHERE id != '__playchl_baseline__'").fetchall()
    games=[]
    for (payload,) in records:
        try:
            game=json.loads(payload)
            if is_target_game(game):games.append(game)
        except (ValueError,TypeError):
            log.warning("Skipping malformed stored game while building /games menu")
    games.sort(key=lambda g:(game_date_key(g),str(g.get('id'))),reverse=True)
    return games

postall_lock=asyncio.Lock()

def unposted_saved_games():
    """Games not yet publicly posted, including the original historical baseline."""
    with db() as conn:
        rows=conn.execute(
            "SELECT payload FROM reports WHERE id != '__playchl_baseline__' "
            "AND (posted_at IS NULL OR posted_at = 'baseline')"
        ).fetchall()
    pending=[]
    for (payload,) in rows:
        try:
            game=json.loads(payload)
            if is_target_game(game):
                validate(game)
                pending.append(game)
        except (ValueError,TypeError,KeyError):
            log.warning("Skipped invalid unposted match while preparing /postall")
    pending.sort(key=lambda g:(game_date_key(g),str(g['id'])))
    return pending

class PostAllConfirmView(discord.ui.View):
    def __init__(self,owner_id):
        super().__init__(timeout=120)
        self.owner_id=owner_id

    async def interaction_check(self,interaction:discord.Interaction):
        if interaction.user.id!=self.owner_id or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only the administrator who requested this can confirm the bulk post.",ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm: Post ALL unposted",style=discord.ButtonStyle.danger)
    async def confirm(self,interaction:discord.Interaction,button:discord.ui.Button):
        if postall_lock.locked():
            return await interaction.response.send_message("A bulk-post operation is already running.",ephemeral=True)
        await interaction.response.defer(ephemeral=True,thinking=True)
        for item in self.children:item.disabled=True
        try:
            async with postall_lock:
                pending=unposted_saved_games()
                posted_count=0
                skipped_count=0
                failed=[]
                for game in pending:
                    try:
                        current,posted=load(game['id'])
                        # Never duplicate an already public post, even if another
                        # command or automatic poll just published the same game.
                        if posted and posted!='baseline':
                            skipped_count+=1
                            continue
                        # Historical baseline is skipped by normal publish; only
                        # explicit administrator confirmation authorizes posting it.
                        was_posted=await publish(current or game,force=(posted=='baseline'))
                        if was_posted:
                            posted_count+=1
                        else:
                            skipped_count+=1
                    except Exception:
                        failed.append(str(game['id']))
                        log.exception("Bulk match-report posting failed for %s",game['id'])
                result=(f"**Bulk publishing complete.** Posted {posted_count} new public report(s) "
                        f"in <#{CHANNEL_ID}>. Skipped {skipped_count} already posted game(s). "
                        f"Failed: {len(failed)}.")
                if failed:
                    result+="\nFailed game IDs: "+", ".join(f"[{gid}]" for gid in failed[:10])
                await interaction.followup.send(result,ephemeral=True)
        except Exception:
            log.exception("Bulk match report action failed")
            await interaction.followup.send("Bulk posting failed; check Railway logs.",ephemeral=True)

    @discord.ui.button(label="Cancel",style=discord.ButtonStyle.secondary)
    async def cancel(self,interaction:discord.Interaction,button:discord.ui.Button):
        for item in self.children:item.disabled=True
        await interaction.response.edit_message(content="Bulk report posting cancelled.",view=None)
        self.stop()

async def show_postall_confirmation(interaction:discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("Manage Server permission is required to post all reports.",ephemeral=True)
    if not CHANNEL_ID:
        return await interaction.response.send_message("REPORT_CHANNEL_ID is not configured.",ephemeral=True)
    count=len(unposted_saved_games())
    if not count:
        return await interaction.response.send_message("All saved Capitals games have already been posted. No duplicate reports will be created.",ephemeral=True)
    await interaction.response.send_message(
        f"**Post all saved Capitals match reports?**\n"
        f"This will post **{count} unposted game(s)** to <#{CHANNEL_ID}> "
        f"**oldest first**, with a separate public card for each game.\n"
        f"Some older games may have incomplete player statistics. "
        f"Previously posted games will not be duplicated.\n"
        f"Select **Confirm** to publish them all.",
        view=PostAllConfirmView(interaction.user.id),ephemeral=True)

clear_channel_lock=asyncio.Lock()

class ClearAnyChannelModal(discord.ui.Modal):
    confirmation=discord.ui.TextInput(
        label="Type DELETE ALL to confirm",placeholder="DELETE ALL",
        required=True,min_length=10,max_length=20)

    def __init__(self,requester_id,channel_id,channel_name,bot_only=False):
        super().__init__(title="Clear Discord channel messages",timeout=180)
        self.requester_id=requester_id
        self.channel_id=channel_id
        self.channel_name=channel_name
        self.bot_only=bot_only
        if bot_only:
            self.confirmation.label="Type DELETE BOT to confirm"
            self.confirmation.placeholder="DELETE BOT"

    async def on_submit(self,interaction:discord.Interaction):
        required="DELETE BOT" if self.bot_only else "DELETE ALL"
        if str(self.confirmation.value).strip().upper()!=required:
            return await interaction.response.send_message("Confirmation did not match. No messages were deleted.",ephemeral=True)
        if not interaction.guild or interaction.user.id!=self.requester_id:
            return await interaction.response.send_message("This confirmation is not valid for your account.",ephemeral=True)
        if not (interaction.user.guild_permissions.manage_guild and interaction.user.guild_permissions.manage_messages):
            return await interaction.response.send_message("Manage Server and Manage Messages permissions are required.",ephemeral=True)
        if clear_channel_lock.locked() or postall_lock.locked():
            return await interaction.response.send_message("A bulk operation is already running. Try again when it finishes.",ephemeral=True)
        channel=interaction.guild.get_channel(self.channel_id)
        if not isinstance(channel,discord.TextChannel):
            return await interaction.response.send_message("This text channel is no longer available. Nothing was deleted.",ephemeral=True)
        user_perms=channel.permissions_for(interaction.user)
        if not user_perms.manage_messages:
            return await interaction.response.send_message("You need Manage Messages permission in this channel.",ephemeral=True)
        me=interaction.guild.me or channel.guild.get_member(bot.user.id)
        if me is None:
            me=await interaction.guild.fetch_member(bot.user.id)
        permissions=channel.permissions_for(me)
        if not (permissions.view_channel and permissions.read_message_history and permissions.manage_messages):
            return await interaction.response.send_message(
                "The bot requires View Channel, Read Message History, and Manage Messages permission in this channel.",ephemeral=True)
        await interaction.response.defer(ephemeral=True,thinking=True)
        count=0
        failed=0
        try:
            async with clear_channel_lock:
                # Delete individually so even messages older than two weeks can be cleared.
                # Operate only on the explicitly confirmed channel, never across the server.
                async for message in channel.history(limit=None,oldest_first=False):
                    if self.bot_only and message.author.id!=bot.user.id:
                        continue
                    try:
                        await message.delete()
                        count+=1
                        # Update report history only when the deleted message actually
                        # belongs to a stored match report; retain all game statistics.
                        with db() as conn:
                            conn.execute(
                                "UPDATE reports SET posted_at=NULL,message_id=NULL,channel_id=NULL "
                                "WHERE channel_id=? AND message_id=?",
                                (str(channel.id),str(message.id)))
                    except discord.NotFound:
                        continue
                    except discord.Forbidden:
                        failed+=1
                        log.exception("Missing permission during channel clear in %s",channel.id)
                        break
                    except discord.HTTPException:
                        failed+=1
                        log.exception("Failed to delete message in channel %s",channel.id)
                await interaction.followup.send(
                    f"**Channel cleanup complete:** <#{channel.id}>\n"
                    f"Deleted **{count}** message(s). Errors: **{failed}**. "
                    f"Mode: **{'bot posts only' if self.bot_only else 'all messages'}**.\n"
                    "Saved match statistics remain available. "
                    "Deleted match reports can be posted again using the postall command.",
                    ephemeral=True)
        except Exception:
            log.exception("Clear channel operation failed in channel %s",self.channel_id)
            await interaction.followup.send(
                f"Channel cleanup stopped after deleting {count} message(s). "
                "Check Railway logs; deleted messages cannot be recovered.",
                ephemeral=True)

@bot.tree.command(name='clearchannel',description='Admin: delete messages in the current Discord text channel after confirmation')
@app_commands.describe(bot_only='True: delete only this bot’s messages; False: delete EVERY message in this channel')
@app_commands.checks.has_permissions(manage_guild=True,manage_messages=True)
async def clearchannel_command(interaction:discord.Interaction,bot_only:bool=False):
    if not interaction.guild or not isinstance(interaction.channel,discord.TextChannel):
        return await interaction.response.send_message("Run this command inside the text channel you want to clear.",ephemeral=True)
    if clear_channel_lock.locked() or postall_lock.locked():
        return await interaction.response.send_message("Another bulk report operation is running.",ephemeral=True)
    if not interaction.channel.permissions_for(interaction.user).manage_messages:
        return await interaction.response.send_message("You cannot manage messages in this channel.",ephemeral=True)
    await interaction.response.send_modal(
        ClearAnyChannelModal(interaction.user.id,interaction.channel.id,interaction.channel.name,bot_only=bot_only))

@bot.tree.command(name='postall',description='Admin: confirm posting every saved but unposted Capitals match report publicly')
@app_commands.checks.has_permissions(manage_guild=True)
async def postall_command(interaction:discord.Interaction):
    await show_postall_confirmation(interaction)

class PostAllMenuButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Post ALL unposted games",style=discord.ButtonStyle.primary,row=2)

    async def callback(self,interaction:discord.Interaction):
        await show_postall_confirmation(interaction)

class GameChoiceSelect(discord.ui.Select):
    def __init__(self,games):
        options=[]
        for g in games:
            date=str(g.get('date') or 'Date unavailable')[:24]
            away=g.get('away',{})
            home=g.get('home',{})
            title=f"{date} • {away.get('name','Away')} {away.get('score','?')}-{home.get('score','?')} {home.get('name','Home')}"
            options.append(discord.SelectOption(
                label=title[:100],value=str(g['id']),
                description=f"PlayCHL game ID: {g['id']}"[:100]))
        super().__init__(placeholder="Select a game to post its report for everyone",
                         min_values=1,max_values=1,options=options,row=0)

    async def callback(self,interaction:discord.Interaction):
        game_id=self.values[0]
        game,_=load(game_id)
        if not game or not is_target_game(game):
            return await interaction.response.send_message("That game is no longer available.",ephemeral=True)
        await interaction.response.defer(ephemeral=True,thinking=True)
        try:
            # Explicit viewer selection posts a public graphic + text report to the
            # configured report channel, even for previously published games.
            await publish(game,force=True)
            with db() as conn:
                row=conn.execute("SELECT message_id,channel_id FROM reports WHERE id=?",(game_id,)).fetchone()
            url=(f"https://discord.com/channels/{interaction.guild_id}/{row[1]}/{row[0]}"
                 if row and row[0] and row[1] and interaction.guild_id else None)
            await interaction.followup.send(
                f"Published **{game.get('date','selected game')}** in <#{CHANNEL_ID}> for everyone to see."
                +(f"\n[View public report]({url})" if url else ""),ephemeral=True)
        except Exception:
            log.exception("Public game selection failed for %s",game_id)
            await interaction.followup.send("Could not post this report; check Railway deploy logs and channel permissions.",ephemeral=True)

class GamePageButton(discord.ui.Button):
    def __init__(self,direction,disabled=False):
        super().__init__(label="← Newer games" if direction<0 else "Older games →",
                         style=discord.ButtonStyle.secondary,disabled=disabled,row=1)
        self.direction=direction

    async def callback(self,interaction:discord.Interaction):
        view=self.view
        if not isinstance(view,GamePickerView):
            return await interaction.response.send_message("Open /games again.",ephemeral=True)
        view.page+=self.direction
        view.populate()
        await interaction.response.edit_message(content=view.page_caption(),view=view)

class GamePickerView(discord.ui.View):
    PAGE_SIZE=25
    def __init__(self,games,owner_id):
        super().__init__(timeout=300)
        self.games=games
        self.owner_id=owner_id
        self.page=0
        self.populate()

    async def interaction_check(self,interaction:discord.Interaction):
        if interaction.user.id!=self.owner_id:
            await interaction.response.send_message("Open /games to choose a report from your own menu.",ephemeral=True)
            return False
        return True

    def populate(self):
        self.clear_items()
        pages=max(1,(len(self.games)+self.PAGE_SIZE-1)//self.PAGE_SIZE)
        self.page=max(0,min(self.page,pages-1))
        batch=self.games[self.page*self.PAGE_SIZE:(self.page+1)*self.PAGE_SIZE]
        if batch:self.add_item(GameChoiceSelect(batch))
        if pages>1:
            self.add_item(GamePageButton(-1,disabled=self.page==0))
            self.add_item(GamePageButton(1,disabled=self.page>=pages-1))
        self.add_item(PostAllMenuButton())

    def page_caption(self):
        pages=max(1,(len(self.games)+self.PAGE_SIZE-1)//self.PAGE_SIZE)
        return (f"🏒 **{TARGET_TEAM_NAME} — Select a Match Report**\n"
                f"{len(self.games)} saved games · Page {self.page+1} of {pages}\n"
                f"Choose a game to generate the graphic and post it publicly in <#{CHANNEL_ID}>.")

@bot.tree.command(name='games',description='Choose any saved Capitals game from a dropdown and post its match card publicly')
async def games_command(interaction:discord.Interaction):
    games=list_saved_games()
    if not games:
        return await interaction.response.send_message(
            "No Capitals games are stored yet. An administrator can use /matchrefresh to import games.",
            ephemeral=True)
    view=GamePickerView(games,interaction.user.id)
    await interaction.response.send_message(view.page_caption(),view=view,ephemeral=True)

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

@bot.tree.command(name='matchrefresh',description='Admin: refresh selected PlayCHL games, or recent schedule if none are configured')
@app_commands.checks.has_permissions(manage_guild=True)
async def matchrefresh(interaction:discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not FEED_URL and not PLAYCHL_PUBLIC:return await interaction.followup.send('No public PlayCHL reader or GAME_FEED_URL enabled. Use /matchimport.',ephemeral=True)
    count=0;errors=0
    try:
        selected_ids=[x.strip() for x in os.getenv('PLAYCHL_EXTRA_GAME_IDS','').split(',') if x.strip()]
        if selected_ids and not FEED_URL:
            # Manual refresh is intentionally scoped to the exact IDs supplied by the admin.
            # Fetch them individually and post oldest-to-newest by the actual game date.
            games=[]
            for game_id in selected_ids:
                try:
                    recovered=await fetch_playchl_games(limit=1,include_id=game_id,team_name=TARGET_TEAM_NAME)
                    games.extend(g for g in recovered if is_target_game(g))
                except Exception:
                    errors+=1
                    log.exception('Selected PlayCHL game fetch failed: %s',game_id)
            games.sort(key=game_date_key)
            log.info('Manual matchrefresh selected %d configured game IDs; %d valid %s games found',len(selected_ids),len(games),TARGET_TEAM_NAME)
        else:
            games=await fetch_feed()
            games.sort(key=game_date_key)
        for g in games:
            try:
                if str(g.get('status','')).lower().startswith('final') and await publish(g):count+=1
            except Exception:
                errors+=1
                log.exception('Game processing error for %s',g.get('id'))
        await interaction.followup.send(f'Checked {len(games)} selected games. New reports posted: {count}. Errors: {errors}.',ephemeral=True)
    except Exception as e:
        log.exception('Manual match refresh failed')
        await interaction.followup.send(f'Feed error: {e}',ephemeral=True)

@bot.tree.error
async def command_error(interaction,error):
    message='Administrator permission required.' if isinstance(error,app_commands.MissingPermissions) else 'Command failed; check Railway logs.'
    if interaction.response.is_done():await interaction.followup.send(message,ephemeral=True)
    else:await interaction.response.send_message(message,ephemeral=True)

if __name__=='__main__':
    if not TOKEN:raise SystemExit('Set DISCORD_TOKEN in Railway environment variables')
    bot.run(TOKEN)
