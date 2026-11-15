"""Public PlayCHL schedule and game-page adapter. No private API or credentials."""
import asyncio
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright

LOG = logging.getLogger("playchl.site")
BASE = "https://playchl.com"
TEAM_CODES = {"ANA":"Anaheim Ducks","BUF":"Buffalo Sabres","CGY":"Calgary Flames",
              "DET":"Detroit Red Wings","MIN":"Minnesota Wild","SEA":"Seattle Kraken",
              "SJS":"San Jose Sharks","UTAH":"Utah Mammoth","VGK":"Vegas Golden Knights",
              "WSH":"Washington Capitals"}
STAT_KEYS = {"Goals":"goals","Shots":"shots","Hits":"hits","TOA":"time_on_attack",
             "Faceoffs%":"faceoff_pct","PIM":"penalty_minutes","Blocks":"blocks",
             "Takeaways":"takeaways","Giveaways":"giveaways","Interceptions":"interceptions"}
SKATER_KEYS = ["position","goals","assists","points","plus_minus","time_on_ice",
               "time_with_puck","shots","passing_pct","faceoffs_won","hits",
               "takeaways","giveaways","blocks","interceptions","penalty_minutes"]
GOALIE_KEYS = ["position","saves","shots_against","goals_against","gaa","save_pct",
               "poke_checks","diving_saves","breakaways","breakaway_pct","penalty_shot_pct"]

def intval(v):
    try: return int(str(v).strip().replace(",",""))
    except (ValueError, TypeError): return 0

def clean_name(s):
    s = re.sub(r"^\\s*\\d{1,3}\\.\\d\\s*", "", str(s or ""))
    return s.strip()

def parse_snapshot(snap, game_id):
    """Normalize text/tables into existing report JSON. Fail closed on incomplete results."""
    text = snap["text"]
    if not re.search(r"\\bFINAL(?:\\s*/\\s*(?:OT|SO))?\\b", text, re.I): return None
    if re.search(r"\\bFORFEIT\\b", text, re.I): return None
    # Site lists two full team names followed by their season records and a score.
    m = re.search(r"([A-Za-z .'-]+)\\n\\d+-\\d+-\\d+\\n(\\d+)\\nFINAL(?:\\s*/\\s*(OT|SO))?[^\\n]*\\n(\\d+)\\n([A-Za-z .'-]+)\\n\\d+-\\d+-\\d+", text, re.I)
    if not m:
        LOG.warning("No verified header for game %s", game_id)
        return None
    away_name,away_score,extra,home_score,home_name = [x.strip() if x else x for x in m.groups()]
    # The site presents visiting team first and home team second.
    game = {"id": game_id,"status": "final/"+extra.lower() if extra else "final",
            "away":{"name":away_name,"score":int(away_score)},
            "home":{"name":home_name,"score":int(home_score)},
            "source_url": f"{BASE}/games/{game_id}",
            "players":[],"team_stats":{"home":{},"away":{}}}
    # Consume table rows captured from the rendered DOM. A label immediately
    # above each table identifies the team and the skater/goalie section.
    for table in snap["tables"]:
        heading = table["heading"].lower()
        side = "away" if away_name.lower() in heading else "home" if home_name.lower() in heading else None
        if side is None: continue
        goalie = "goalie" in heading
        skater = "skater" in heading
        if not (goalie or skater): continue
        keys = GOALIE_KEYS if goalie else SKATER_KEYS
        for row in table["rows"]:
            if len(row) < (8 if goalie else 12): continue
            name = clean_name(row[0])
            if not name or name.lower()=="player": continue
            player={"name":name,"team":game[side]["name"],"position":"G" if goalie else row[1]}
            for key,value in zip(keys,row[1:]):
                if key=="position": continue
                if key in ("time_on_ice","time_with_puck","passing_pct","breakaway_pct","penalty_shot_pct"):
                    player[key]=value
                elif key in ("gaa","save_pct"): 
                    try: player[key]=float(value.replace("%",""))
                    except ValueError: pass
                else: player[key]=intval(value)
            # Player score is shown inside the first cell; preserved if present.
            score_match=re.match(r"\\s*(\\d{1,3}\\.\\d)",row[0])
            if score_match:player["player_score"]=float(score_match.group(1))
            game["players"].append(player)
    # Section text makes the official 3-star names/positions available.
    stars=[]
    for i in (1,2,3):
        pattern=rf"{i}(?:st|nd|rd) Star\\s+([^\\n]+)\\n([^\\n]+)\\n([^\\n]+)\\n([\\d.]+)\\s*Player Score"
        match=re.search(pattern,text,re.I)
        if match:
            name,pos,line,score=match.groups()
            stars.append({"name":name.strip(),"position":pos.strip(),
                          "stat_line":line.strip(),"player_score":float(score)})
    game["official_stars"]=stars
    # A complete player box score is mandatory before auto-publication.
    if len(game["players"])<8 or len(stars)!=3:
        LOG.warning("Incomplete game %s: players=%s stars=%s",game_id,len(game["players"]),len(stars))
        return None
    # Match Stats is often a non-table two-column section.
    sm = re.search(r"Match Stats\\n(.*?)(?:EA Sports API Results|(?:[A-Za-z ]+ Skaters))",text,re.I|re.S)
    if sm:
        lines=[x.strip() for x in sm.group(1).splitlines() if x.strip()]
        for i,line in enumerate(lines):
            key=STAT_KEYS.get(line)
            if key and i>0 and i+1<len(lines):
                game["team_stats"]["away"][key]=lines[i-1]
                game["team_stats"]["home"][key]=lines[i+1]
    return game

EXTRACT_JS = """() => {
const text=document.body.innerText;
const tables=[...document.querySelectorAll('table')].map(t=>{
 let prev=t, heading='';
 for(let i=0;i<7&&prev;i++){
   prev=prev.previousElementSibling||prev.parentElement?.previousElementSibling;
   if(prev){const val=(prev.innerText||'').trim();if(/(skaters|goalies)/i.test(val)&&val.length<180){heading=val;break}}
 }
 if(!heading){
   let node=t.parentElement;
   for(let i=0;i<5&&node;i++,node=node.parentElement){
     const s=(node.innerText||'').slice(0,200);
     const m=s.match(/([^\\n]+(?:Skaters|Goalies))/i);
     if(m){heading=m[1];break}
   }
 }
 const rows=[...t.querySelectorAll('tbody tr')].map(tr=>[...tr.querySelectorAll('td')].map(td=>td.innerText.trim()));
 return {heading,rows};
});
const links=[...document.querySelectorAll('a[href*="/games/"]')].map(a=>({href:a.getAttribute('href'),text:(a.innerText||'').trim()}));
return {text,tables,links};
}"""

async def get_page(browser,url):
    page=await browser.new_page(viewport={"width":1440,"height":1000})
    try:
        await page.goto(url,wait_until="domcontentloaded",timeout=35000)
        await page.wait_for_timeout(1500)
        return await page.evaluate(EXTRACT_JS)
    finally:await page.close()

async def fetch_playchl_games(limit=25,include_id=None):
    results=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=["--no-sandbox"])
        try:
            schedule=await get_page(browser,f"{BASE}/schedule")
            ids=[]
            for link in schedule["links"]:
                m=re.search(r"/games/([a-fA-F0-9]{24})",link["href"] or "")
                if m and m.group(1) not in ids:ids.append(m.group(1))
            if include_id:ids=[include_id]
            else:ids=ids[-limit:]
            LOG.info("PlayCHL schedule discovered %s games, checking %s",len(schedule["links"]),len(ids))
            for gid in ids:
                try:
                    snap=await get_page(browser,f"{BASE}/games/{gid}")
                    game=parse_snapshot(snap,gid)
                    if game:results.append(game)
                except Exception:LOG.exception("Unable to parse PlayCHL game %s",gid)
        finally:await browser.close()
    return results
