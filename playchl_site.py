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
    lines = [x.strip() for x in str(s or "").splitlines() if x.strip()]
    lines = [x for x in lines if not re.fullmatch(r"\d{1,3}\.\d|Xbox|PlayStation|PC",x,re.I)]
    return lines[-1] if lines else ""

def parse_snapshot(snap, game_id):
    """Normalize text/tables into existing report JSON. Fail closed on incomplete results."""
    text = snap["text"]
    if not re.search(r"\bFINAL(?:\s*/\s*(?:OT|SO))?\b", text, re.I): return None
    if re.search(r"\bFORFEIT\b", text, re.I): return None
    # The game header is rendered as a responsive layout, so innerText order
    # can vary by browser. Resolve scores around FINAL and team names by proximity.
    lines=[x.strip() for x in text.splitlines() if x.strip()]
    final_idx=next((i for i,line in enumerate(lines) if re.search(r"\bFINAL\b",line,re.I)),None)
    if final_idx is None:
        LOG.warning("No FINAL marker for game %s; page text=%r",game_id,text[:900])
        return None
    final_line=lines[final_idx]
    inline=re.search(r"(\d+)\s*FINAL(?:\s*/\s*(OT|SO))?\s*(\d+)",final_line,re.I)
    extra=None
    if inline:
        away_score,extra,home_score=inline.groups()
    else:
        before=next((x for x in reversed(lines[:final_idx]) if re.fullmatch(r"\d+",x)),None)
        after=next((x for x in lines[final_idx+1:] if re.fullmatch(r"\d+",x)),None)
        extra_match=re.search(r"FINAL\s*/\s*(OT|SO)",final_line,re.I)
        extra=extra_match.group(1) if extra_match else None
        away_score,home_score=before,after
    def team_candidate(seq):
        ignored=("continental hockey league","final","player","skaters","goalies","match stats",
                 "puck possession","match share","deserve to win","cold players","ea sports")
        for value in seq:
            value=value.strip(" •|")
            if (2<=len(value)<=42 and re.search(r"[A-Za-z]",value)
                and not re.search(r"\d",value)
                and not any(term in value.lower() for term in ignored)):
                return value
        return None
    away_name=team_candidate(reversed(lines[:final_idx]))
    home_name=team_candidate(lines[final_idx+1:])
    if not (away_name and home_name and away_score is not None and home_score is not None):
        LOG.warning("Could not resolve game header for %s; final_line=%r preceding=%r following=%r",
                    game_id,final_line,lines[max(0,final_idx-5):final_idx],lines[final_idx+1:final_idx+6])
        return None
    away_score,home_score=int(away_score),int(home_score)
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
            score_match=re.match(r"\s*(\d{1,3}\.\d)",row[0])
            if score_match:player["player_score"]=float(score_match.group(1))
            game["players"].append(player)
    # Section text makes the official 3-star names/positions available.
    stars=[]
    for i in (1,2,3):
        pattern=rf"{i}(?:st|nd|rd) Star\s+([^\n]+)\n([^\n]+)\n([^\n]+)\n([\d.]+)\s*Player Score"
        match=re.search(pattern,text,re.I)
        if match:
            name,pos,line,score=match.groups()
            stars.append({"name":name.strip(),"position":pos.strip(),
                          "stat_line":line.strip(),"player_score":float(score)})
    game["official_stars"]=stars
    # A complete player box score is mandatory before auto-publication.
    if len(game["players"])<8 or len(stars)!=3:
        LOG.warning("Incomplete game %s: players=%s stars=%s table_headings=%r table_rows=%r",game_id,len(game["players"]),len(stars),[t["heading"] for t in snap["tables"]],[len(t["rows"]) for t in snap["tables"]])
        return None
    # Match Stats is often a non-table two-column section.
    sm = re.search(r"Match Stats\n(.*?)(?:EA Sports API Results|(?:[A-Za-z ]+ Skaters))",text,re.I|re.S)
    if sm:
        lines=[x.strip() for x in sm.group(1).splitlines() if x.strip()]
        for i,line in enumerate(lines):
            key=STAT_KEYS.get(line)
            if key and i>0 and i+1<len(lines):
                game["team_stats"]["away"][key]=lines[i-1]
                game["team_stats"]["home"][key]=lines[i+1]
    return game

EXTRACT_JS = r"""() => {
const text=document.body.innerText;
const getCells = row => {
  let cells=[...row.querySelectorAll(':scope > td,:scope > th,:scope > [role="cell"],:scope > [role="gridcell"],:scope > div')];
  if(cells.length<6) cells=[...row.children];
  return cells.map(c=>(c.innerText||c.textContent||'').trim()).filter(Boolean);
};
const rowEls=[...document.querySelectorAll('tr,[role="row"],[class*="player-row"],[class*="playerRow"],[class*="grid-row"],[class*="stat-row"]')];
const tables=[];
for(const table of document.querySelectorAll('table,[role="table"],[role="grid"]')){
  let heading='';
  let node=table;
  for(let i=0;i<7&&node;i++,node=node.parentElement){
    const prev=node.previousElementSibling;
    const val=(prev?.innerText||'').trim();
    if(/(skaters|goalies)/i.test(val)&&val.length<180){heading=val;break}
    const m=(node.innerText||'').slice(0,300).match(/([^\n]+(?:Skaters|Goalies))/i);
    if(m){heading=m[1];break}
  }
  const rows=[...table.querySelectorAll('tr,[role="row"]')].map(getCells).filter(r=>r.length>=6);
  tables.push({heading,rows});
}
for(const row of rowEls){
  const cells=getCells(row);
  if(cells.length<10)continue;
  let heading='';
  let node=row;
  for(let i=0;i<8&&node;i++,node=node.parentElement){
    const s=(node.innerText||'').slice(0,700);
    const m=s.match(/([^\n]{2,70}(?:Skaters|Goalies))/i);
    if(m){heading=m[1];break}
  }
  if(!tables.some(t=>t.rows.some(r=>r.join('|')===cells.join('|')))){
    tables.push({heading,rows:[cells]});
  }
}
const links=[...document.querySelectorAll('a[href*="/games/"]')].map(a=>{
 let node=a,context='';
 for(let i=0;i<8&&node;i++,node=node.parentElement){
   const alts=[...node.querySelectorAll('img')].map(img=>img.alt||'').join(' ');
   const candidate=((node.innerText||'')+' '+alts).trim();
   if(/Washington|Capitals|\bWSH\b/i.test(candidate)&&candidate.length<1600){context=candidate;break}
 }
 return {href:a.getAttribute('href'),text:(a.innerText||'').trim(),context};
});
return {text,tables,links};
}"""

async def get_page(browser,url):
    page=await browser.new_page(viewport={"width":1440,"height":1000})
    try:
        await page.goto(url,wait_until="domcontentloaded",timeout=35000)
        try: await page.wait_for_function("document.body && /\\bFINAL\\b/i.test(document.body.innerText)",timeout=12000)
        except Exception: await page.wait_for_timeout(2500)
        return await page.evaluate(EXTRACT_JS)
    finally:await page.close()

async def fetch_playchl_games(limit=25,include_id=None,team_name=None):
    results=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=["--no-sandbox"])
        try:
            schedule=await get_page(browser,f"{BASE}/schedule")
            ids=[]
            matching_ids=[]
            for link in schedule["links"]:
                m=re.search(r"/games/([a-fA-F0-9]{24})",link["href"] or "")
                if m and m.group(1) not in ids:
                    ids.append(m.group(1))
                    context=(link.get("context","")+" "+link.get("text","")).casefold()
                    if team_name and ("washington" in context or "capitals" in context or re.search(r"\bwsh\b",context)):
                        matching_ids.append(m.group(1))
            if include_id:ids=[include_id]
            elif team_name:ids=(matching_ids or ids[-limit:])[-limit:]
            else:ids=ids[-limit:]
            LOG.info("PlayCHL schedule discovered %s games; team=%s; checking %s candidate games",
                     len(schedule["links"]),team_name or "all teams",len(ids))
            for gid in ids:
                try:
                    snap=await get_page(browser,f"{BASE}/games/{gid}")
                    game=parse_snapshot(snap,gid)
                    if game and team_name:
                        names=[str(game.get(side,{}).get("name","")).casefold() for side in ("home","away")]
                        is_target=any(("washington" in n or "capitals" in n or n.strip()=="wsh") for n in names)
                        if not is_target:
                            LOG.info("Skipping non-%s game %s: %s",team_name,gid,names)
                            continue
                    if game:results.append(game)
                except Exception:LOG.exception("Unable to parse PlayCHL game %s",gid)
        finally:await browser.close()
    return results
