"""PlayCHL match reports. Manual imports + optional verified game JSON feed polling."""
import asyncio
import csv
import io
import json
import logging
import os
import re
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
PLAYCHL_LOOKBACK=max(1,min(100,int(os.getenv('PLAYCHL_LOOKBACK','50'))))
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
    csv_bytes=build_boxscore_csv(game)
    if not csv_bytes:return await interaction.response.send_message('No individual player box stats for this game.',ephemeral=True)

    await interaction.response.send_message(file=discord.File(io.BytesIO(csv_bytes),filename=f'boxstats_{game_id[:50]}.csv'),ephemeral=True)

@bot.tree.command(name='matchhistory',description='Show recent imported games')
async def matchhistory(interaction:discord.Interaction):
    with db() as c:rows=c.execute("SELECT payload,posted_at FROM reports WHERE id != '__playchl_baseline__' ORDER BY COALESCE(posted_at, '') DESC LIMIT 15").fetchall()
    lines=[f"`{json.loads(p)['id']}` • {summary(json.loads(p))} • {'Posted' if posted else 'Not posted'}" for p,posted in rows if is_target_game(json.loads(p))]
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
