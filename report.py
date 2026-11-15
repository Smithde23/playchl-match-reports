"""Box score validation, star selection, and report presentation."""
from __future__ import annotations
import io
from typing import Any
from PIL import Image, ImageDraw, ImageFont

TEAMS = {"ANA","BUF","CGY","DET","MIN","SEA","SJS","UTAH","VGK","WSH"}

def num(value, default=0):
    try: return float(value)
    except (ValueError, TypeError): return float(default)

def label(v): return str(v if v is not None else '—')

def validate(game: dict) -> dict:
    if not isinstance(game, dict): raise ValueError('Game payload must be a JSON object.')
    for k in ('id','home','away','status'):
        if k not in game: raise ValueError(f'Missing required field: {k}')
    if str(game['status']).lower() not in ('final','final/ot','final/so'):
        raise ValueError('Only finalized games may be published.')
    if not str(game['id']).strip() or len(str(game['id'])) > 120: raise ValueError('Invalid game ID')
    for side in ('home','away'):
        team=game[side]
        if not isinstance(team,dict) or not team.get('name') or not isinstance(team.get('score'), int) or team['score'] < 0:
            raise ValueError(f'{side} needs name and a nonnegative integer score')
    if game['home']['name']==game['away']['name']: raise ValueError('Teams must be different')
    for row in game.get('players',[]):
        if not isinstance(row,dict) or not row.get('name') or not row.get('team'):
            raise ValueError('Every player needs name and team')
        if row['team'] not in (game['home']['name'], game['away']['name']):
            raise ValueError('Player team must match home or away team')
    return game

def player_score(p:dict, winner: str) -> float:
    pos=str(p.get('position','')).upper()
    winning=1.5 if p.get('team')==winner else 0
    if pos in ('G','GOL','GOALIE'):
        shots=num(p.get('shots_against'))
        saves=num(p.get('saves'))
        goals_against=num(p.get('goals_against'), max(shots-saves,0))
        return winning + (saves/shots*20 if shots else 0) + saves*.20 - goals_against*2.2 + num(p.get('shutout'))*8
    return (num(p.get('goals'))*5+num(p.get('assists'))*3 + num(p.get('plus_minus'))*.3
            + num(p.get('shots'))*.20 + num(p.get('hits'))*.07 + num(p.get('takeaways'))*.12
            - num(p.get('penalty_minutes'))*.07 + winning)

def three_stars(game: dict) -> list[dict]:
    validate(game)
    if game.get('forfeit'): return []
    if len(game.get('official_stars') or []) == 3:
        result=[]
        for s in game['official_stars']:
            p=next((x for x in game.get('players',[]) if x['name'].casefold()==s['name'].casefold()),None)
            result.append({**(p or {'team':'PlayCHL','position':s.get('position','')}),**s,'official':True})
        return result
    players=game.get('players') or []
    if not players: return []
    h,a=game['home'],game['away']
    winner=h['name'] if h['score']>a['score'] else a['name'] if a['score']>h['score'] else ''
    ranked=sorted(players,key=lambda p:(player_score(p,winner),num(p.get('goals')),num(p.get('assists')),str(p['name']).lower()),reverse=True)
    seen=set(); result=[]
    for p in ranked:
        key=(p['team'],p['name'].casefold())
        if key in seen: continue
        seen.add(key);result.append(p)
        if len(result)==3:break
    return result

def stat_line(p:dict) -> str:
    if p.get('official') and p.get('stat_line'): return p['stat_line']
    if str(p.get('position','')).upper() in ('G','GOL','GOALIE'):
        return f"{label(p.get('saves'))} SV · {label(p.get('shots_against'))} SA · {label(p.get('save_pct'))} SV%"
    return f"{label(p.get('goals'))} G · {label(p.get('assists'))} A · {label(p.get('points',int(num(p.get('goals'))+num(p.get('assists')))))} PTS"

def summary(game):
    h,a=game['home'],game['away']
    return f"{a['name']} {a['score']} — {h['name']} {h['score']} ({game['status'].upper()})"

def font(size, bold=False):
    path='/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    try:return ImageFont.truetype(path,size)
    except OSError:return ImageFont.load_default()

def render_card(game:dict)->bytes:
    """Readable high-resolution report with available statistics only; no invented values."""
    validate(game)
    W=1200; h,a=game['home'],game['away']; players=game.get('players',[])
    home_players=[p for p in players if p['team']==h['name']]
    away_players=[p for p in players if p['team']==a['name']]
    stats=game.get('team_stats') or {}
    pairs=[('Goals','goals'),('Shots','shots'),('Hits','hits'),('Faceoff %','faceoff_pct'),('PIM','penalty_minutes'),('Blocks','blocks'),('Takeaways','takeaways'),('Giveaways','giveaways'),('Interceptions','interceptions'),('Time on attack','time_on_attack')]
    avail=[(name,key) for name,key in pairs if key in stats.get('home',{}) or key in stats.get('away',{})]
    stars=three_stars(game)
    height=450+max(70,len(avail)*40)+205+max(len(home_players),len(away_players),1)*34+145
    im=Image.new('RGB',(W,height),(12,20,34));d=ImageDraw.Draw(im)
    navy=(12,20,34);panel=(26,40,61);white=(240,246,252);muted=(157,179,204);gold=(245,194,73);blue=(63,162,222)
    def box(xy, fill=panel,r=18):d.rounded_rectangle(xy,radius=r,fill=fill)
    def txt(x,y,s,size=24,fill=white,bold=False,anchor=None):d.text((x,y),str(s),font=font(size,bold),fill=fill,anchor=anchor)
    txt(45,30,'PLAYCHL  /  OFFICIAL MATCH REPORT',27,blue,True)
    txt(45,77,f"GAME {game['id']}  ·  {game.get('date','Date unavailable')}  ·  {game['status'].upper()}",19,muted)
    box((35,120,W-35,275))
    # Keep team names and score in separate fixed-width zones to prevent overlap.
    txt(245,143,'AWAY',16,muted,True,'mt')
    txt(955,143,'HOME',16,muted,True,'mt')
    def centered_fit(cx,top,value,max_width=330,size=34,fill=white,bold=True):
        chosen=size
        while chosen>20 and d.textbbox((0,0),str(value),font=font(chosen,bold))[2]>max_width:
            chosen-=1
        d.text((cx,top),str(value),font=font(chosen,bold),fill=fill,anchor='mt')
    centered_fit(245,178,a['name'],330,34)
    centered_fit(955,178,h['name'],330,34)
    centered_fit(W//2,164,f"{a['score']}  –  {h['score']}",260,60,gold,True)
    txt(45,300,'THREE STARS OF THE GAME',27,gold,True)
    if stars:
        for i,p in enumerate(stars):
            x=35+i*380;box((x,345,x+365,435))
            txt(x+18,358,f"#{i+1}  {p['name'][:20]}",22,gold if i==0 else white,True)
            txt(x+18,392,f"{p['team']}  •  {stat_line(p)[:36]}",17,muted)
    else:txt(45,362,'Not available (missing player box stats or forfeit)',20,muted)
    y=462;txt(45,y,'TEAM BOX SCORE',26,blue,True);y+=48
    box((35,y,W-35,y+48+len(avail)*40))
    txt(100,y+12,a['name'],21,white,True)
    txt(W-100,y+12,h['name'],21,white,True,'ra')
    for i,(name,key) in enumerate(avail):
        yy=y+55+i*40;txt(100,yy,label(stats.get('away',{}).get(key)),22,white,True)
        txt(W//2,yy,name,19,muted,False,'mt')
        txt(W-100,yy,label(stats.get('home',{}).get(key)),22,white,True,'ra')
    if not avail:txt(65,y+61,'Team statistics not supplied',21,muted)
    y+=75+len(avail)*40;txt(45,y,'PLAYER BOX SCORE',26,blue,True);y+=44
    box((35,y,W-35,y+68+max(len(home_players),len(away_players),1)*34))
    txt(62,y+12,f"{a['name']} PLAYERS",20,white,True)
    txt(635,y+12,f"{h['name']} PLAYERS",20,white,True)
    txt(62,y+36,'PLAYER                  POS   G-A-P    +/-   S   H  BLK   TOI',13,muted,True)
    txt(635,y+36,'PLAYER                  POS   G-A-P    +/-   S   H  BLK   TOI',13,muted,True)
    def compact(p):
        pos=str(p.get('position','—')).upper()
        name=str(p['name'])[:18]
        if pos in ('G','GOL','GOALIE'):
            return f"{name:18}  G  SV {label(p.get('saves')):>2}/{label(p.get('shots_against')):<2} GA {label(p.get('goals_against')):>2}  SV% {label(p.get('save_pct'))}"
        pts=label(p.get('points',int(num(p.get('goals'))+num(p.get('assists')))))
        toi=str(p.get('time_on_ice','—'))[:5]
        return f"{name:18} {pos:>2} {label(p.get('goals'))}-{label(p.get('assists'))}-{pts} {num(p.get('plus_minus')):+.0f} {num(p.get('shots')):>2.0f} {num(p.get('hits')):>2.0f} {num(p.get('blocks')):>2.0f} {toi:>5}"
    for i in range(max(len(home_players),len(away_players),1)):
        yy=y+76+i*34
        if i<len(away_players):txt(62,yy,compact(away_players[i]),13,white)
        if i<len(home_players):txt(635,yy,compact(home_players[i]),13,white)
    txt(45,height-75,'FULL PLAYER BOX SCORE: CSV attached to this Discord report',18,muted)
    txt(45,height-43,'Only supplied stats shown. Stars: PlayCHL official when available.',16,muted)
    buffer=io.BytesIO();im.save(buffer,format='PNG');return buffer.getvalue()
