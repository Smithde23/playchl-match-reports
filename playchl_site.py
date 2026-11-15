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
    date_match=re.search(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4},\s+\d{1,2}:\d{2}\s*[AP]M\b",text,re.I)
    if date_match:game["date"]=date_match.group(0)
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
    # A final score is enough to preserve the game. Box-score data may hydrate later;
    # the bot retries incomplete pages and updates the existing Discord card when richer data arrives.
    game["boxscore_complete"] = len(game["players"]) >= 8 and len(stars) == 3
    game["boxscore_players_count"] = len(game["players"])
    if not game["boxscore_complete"]:
        LOG.warning("Partial final game %s retained for retry: players=%s stars=%s table_headings=%r table_rows=%r",
                    game_id,len(game["players"]),len(stars),[t["heading"] for t in snap["tables"]],[len(t["rows"]) for t in snap["tables"]])
    # Team stats are rendered as value / label / value, sometimes with line breaks.
    stat_labels={"Goals":"goals","Shots":"shots","Hits":"hits","TOA":"time_on_attack",
                 "Faceoffs%":"faceoff_pct","PIM":"penalty_minutes","Blocks":"blocks",
                 "Takeaways":"takeaways","Giveaways":"giveaways","Interceptions":"interceptions"}
    value_pattern=r"([+-]?\d+(?::\d{2})?%?)"
    for label,key in stat_labels.items():
        match=re.search(value_pattern+r"\s*"+re.escape(label)+r"\s*"+value_pattern,text,re.I)
        if match:
            game["team_stats"]["away"][key]=match.group(1)
            game["team_stats"]["home"][key]=match.group(2)
    return game

EXTRACT_JS = r"""() => {
const text=document.body.innerText || '';
const norm=s=>(s||'').replace(/\s+/g,' ').trim();
const getCells=row=>{
  let kids=[...row.children].filter(el=>norm(el.innerText||el.textContent));
  if(kids.length<6)kids=[...row.querySelectorAll(':scope > td,:scope > th,:scope > [role="cell"],:scope > [role="gridcell"],:scope > div')].filter(el=>norm(el.innerText||el.textContent));
  return kids.map(el=>norm(el.innerText||el.textContent)).filter(Boolean);
};
const isSection=s=>/(skaters|goalies)/i.test(s||'');
const sectionHeading=node=>{
  let cur=node;
  for(let depth=0;depth<10&&cur;depth++,cur=cur.parentElement){
    const lines=(cur.innerText||'').split(/\n/).map(x=>x.trim()).filter(Boolean);
    const found=lines.find(line=>line.length<120&&isSection(line)&&!/^player$/i.test(line));
    if(found)return found;
    let prev=cur.previousElementSibling;
    for(let n=0;n<3&&prev;n++,prev=prev.previousElementSibling){
      const val=(prev.innerText||'').trim();
      if(val.length<160&&isSection(val))return val;
    }
  }
  return '';
};
const grouped=new Map();
const candidates=[...document.querySelectorAll('tr,[role="row"],[class*="row"],[class*="Row"],[class*="grid"],[class*="Grid"],[class*="flex"],[class*="Flex"]')];
for(const node of candidates){
  const cells=getCells(node);
  if(cells.length<10||cells.length>28)continue;
  const heading=sectionHeading(node);
  if(!heading)continue;
  const key=heading.toLowerCase();
  if(!grouped.has(key))grouped.set(key,{heading,rows:[]});
  const bucket=grouped.get(key);
  const signature=cells.join('|');
  if(!bucket.rows.some(row=>row.join('|')===signature))bucket.rows.push(cells);
}
const tables=[...grouped.values()];
const links=[...document.querySelectorAll('a[href*="/games/"]')].map(a=>{
 let node=a,context='';
 for(let i=0;i<12&&node;i++,node=node.parentElement){
   const alts=[...node.querySelectorAll('img')].map(img=>img.alt||'').join(' ');
   const candidate=((node.innerText||'')+' '+alts).trim();
   if(/Washington|Capitals|\bWSH\b/i.test(candidate)&&candidate.length<5000){context=candidate;break}
 }
 return {href:a.getAttribute('href'),text:(a.innerText||'').trim(),context};
});
return {text,tables,links};
}"""

async def get_page(browser,url,wait_for_games=False):
    page=await browser.new_page(viewport={"width":1440,"height":1000})
    try:
        await page.goto(url,wait_until="domcontentloaded",timeout=35000)
        if wait_for_games:
            try:
                await page.wait_for_function("document.querySelectorAll('a[href*=\"/games/\"]').length > 0",timeout=25000)
            except Exception:
                await page.wait_for_timeout(5000)
        else:
            try: await page.wait_for_function("document.body && /\\bFINAL(?:\\s*\\/\\s*(?:OT|SO))?\\b/i.test(document.body.innerText)",timeout=15000)
            except Exception: await page.wait_for_timeout(2500)
            # PlayCHL renders the scoreboard first and hydrates skater/goalie panels afterward.
            try:
                await page.wait_for_function("""() => {
                    const t=document.body?.innerText||'';
                    const hasSections=/skaters/i.test(t)||/goalies/i.test(t);
                    const hasRows=document.querySelectorAll('tr,[role="row"],[class*="row"],[class*="Row"],[class*="grid"],[class*="flex"]').length>=8;
                    const hasStars=/1st Star/i.test(t)&&/2nd Star/i.test(t);
                    return (hasSections&&hasRows)||hasStars;
                }""",timeout=10000)
            except Exception:
                LOG.debug("Player/goalie panels did not become detectable before timeout: %s",url)
            await page.wait_for_timeout(1200)
        return await page.evaluate(EXTRACT_JS)
    finally:await page.close()

def _date_key(value):
    """Parse PlayCHL's displayed game date for true chronological sorting."""
    value=str(value or "")
    patterns=[
        (r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4},\s+\d{1,2}:\d{2}\s*[AP]M", "%b %d, %Y, %I:%M %p"),
        (r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}", "%b %d, %Y"),
        (r"\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s*[AP]M", "%m/%d/%Y %I:%M %p"),
        (r"\d{1,2}/\d{1,2}/\d{4}", "%m/%d/%Y"),
        (r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?", None),
        (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
    ]
    for pattern,fmt in patterns:
        match=re.search(pattern,value,re.I)
        if not match:continue
        raw=match.group(0)
        try:
            if fmt:
                dt=datetime.strptime(raw,fmt)
            else:
                dt=datetime.fromisoformat(raw.replace("Z","+00:00"))
            if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return datetime.min.replace(tzinfo=timezone.utc)

async def fetch_playchl_games(limit=10,include_id=None,team_name=None):
    """Fetch final games. limit=None scans every accessible matching schedule card."""

    results=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=["--no-sandbox"])
        try:
            # A direct game ID does not require loading the entire schedule.
            schedule={"links":[]}
            if not include_id:
                for schedule_attempt in range(2):
                    schedule=await get_page(browser,f"{BASE}/schedule",wait_for_games=True)
                    if schedule.get("links"):
                        break
                    LOG.warning("PlayCHL schedule contained no game links; retry %s/2",schedule_attempt+1)
                    await asyncio.sleep(3)
                if not schedule.get("links"):
                    raise RuntimeError("PlayCHL schedule returned no game links; cannot confirm full history")
            entries=[]
            seen=set()
            for link in schedule["links"]:
                match=re.search(r"/games/([a-fA-F0-9]{24})",link.get("href") or "")
                if not match:continue
                gid=match.group(1)
                if gid in seen:continue
                seen.add(gid)
                context=(link.get("context","")+" "+link.get("text","")).strip()
                is_team=bool(team_name and re.search(r"Washington|Capitals|\bWSH\b",context,re.I))
                date_match=re.search(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4},\s+\d{1,2}:\d{2}\s*[AP]M\b",context,re.I)
                date_text=date_match.group(0) if date_match else ""
                entries.append({"id":gid,"context":context,"is_team":is_team,
                                "date_text":date_text,"date":_date_key(context)})
            if include_id:
                candidates=[{"id":include_id,"context":"","date":datetime.min.replace(tzinfo=timezone.utc)}]
            else:
                team_entries=[entry for entry in entries if entry["is_team"]]
                if team_name and team_entries:
                    candidates=team_entries
                else:
                    candidates=entries
                candidates.sort(key=lambda entry:entry["date"],reverse=True)
                # Check a larger candidate window, then select the newest complete final games.
                candidate_limit=max(limit*10,100) if limit else len(candidates)
                candidates=candidates[:candidate_limit]
            LOG.info("PlayCHL schedule discovered %s unique game links; %s team cards matched; checking %s candidates for newest %s complete games",
                     len(entries),sum(1 for entry in entries if entry["is_team"]),len(candidates),limit)
            semaphore=asyncio.Semaphore(5)
            async def fetch_one(entry):
                gid=entry["id"]
                async with semaphore:
                    best=None
                    best_quality=-1
                    for attempt in range(1,3):
                        try:
                            snap=await get_page(browser,f"{BASE}/games/{gid}")
                            game=parse_snapshot(snap,gid)
                            if game and not game.get("date") and entry.get("date_text"):
                                game["date"]=entry["date_text"]
                            if game and team_name:
                                names=[str(game.get(side,{}).get("name","")).casefold() for side in ("home","away")]
                                is_target=any(("washington" in name or "capitals" in name or name.strip()=="wsh") for name in names)
                                if not is_target:
                                    LOG.info("Skipping non-%s game %s: %s",team_name,gid,names)
                                    return None
                            if game:
                                quality=len(game.get("players") or [])*10+len(game.get("official_stars") or [])*100
                                if quality>best_quality:
                                    best,best_quality=game,quality
                                if game.get("boxscore_complete"):
                                    return game
                            if attempt<2 and (not game or not game.get("boxscore_complete")):
                                LOG.info("Retrying incomplete PlayCHL game %s (attempt %s/2)",gid,attempt+1)
                                await asyncio.sleep(2)
                        except Exception:
                            LOG.exception("Unable to parse PlayCHL game %s (attempt %s)",gid,attempt)
                            if attempt<2:await asyncio.sleep(2)
                    return best
            fetched=await asyncio.gather(*(fetch_one(entry) for entry in candidates))
            results.extend(game for game in fetched if game)
        finally:
            await browser.close()
    results.sort(key=lambda game:_date_key(game.get("date")),reverse=True)
    return results[:limit] if limit and not include_id else results
