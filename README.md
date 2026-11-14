# PlayCHL Match Report Bot

A **separate** Discord bot for posting an illustrated match report after each finalized game: score, available team box stats, roster box stats and algorithmic 1st/2nd/3rd stars. No fake statistics are generated.

## Important: where the game stats come from

The public PlayCHL website publishes scores and season statistics, but a reliable publicly documented **per-game full box-score API** was **not verified** during development. The league rules describe team managers uploading game statistics through MSO. This bot therefore supports:

1. **Working now:** `/matchimport` + `game.example.json`-style full per-game JSON, submitted by an authorized Discord admin. It immediately posts the card.
2. **Automatic after each game once connected:** `GAME_FEED_URL` set to a verified HTTPS JSON feed that returns finalized game objects in the same format. The bot polls every `POLL_MINUTES`, posts newly finalized games once, and remembers previously posted IDs.

Do not set `GAME_FEED_URL=https://playchl.com` — the website homepage does **not** conform to the expected box-score JSON schema. A verified MSO/ehockey exporter or a custom bridge is needed for fully unattended reports. Never invent player performances from season averages.

## Discord commands

| Command | Permission | Action |
|---|---|---|
| `/matchimport file publish_now` | Manage Server | Import full game JSON and publish report |
| `/matchreport game_id` | Everyone | Preview a previous report card |
| `/matchstats game_id` | Everyone | Export full available player box-score columns as CSV |
| `/matchhistory` | Everyone | List 15 most recent imported games |
| `/matchrepost game_id` | Manage Server | Publish again |
| `/matchrefresh` | Manage Server | Fetch configured feed immediately |

The graphic highlights the game score, available team box stats and a compact roster table. The `/matchstats` CSV holds *all supported per-player raw numeric fields*. The three-star formula accounts for goals, assists, +/- and other gameplay stats. Goalies use saves/save percentage/goals against. Stars are selected from the supplied stats, not official EA NHL stars.

## GitHub + Railway deployment

1. Create a **new private GitHub repository** such as `playchl-match-reports`; upload all files in this folder to its root.
2. In [Discord Developer Portal](https://discord.com/developers/applications), create a **new application** and bot. Invite it with `bot` and `applications.commands` scopes. Give it permission to View Channel, Send Messages, Embed Links and Attach Files. Copy the new bot token privately to Railway. **Never commit the token to GitHub or send it in chat.** If a token was previously exposed, reset it in Discord first.
3. In Railway, create **a separate service** from this repository, using its provided `Dockerfile`.
4. Set Railway environment variables from `.env.example`: `DISCORD_TOKEN`, `GUILD_ID`, `REPORT_CHANNEL_ID`, `DATABASE_PATH=/data/matchreports.sqlite3` and optional `GAME_FEED_URL`/`GAME_FEED_TOKEN`.
5. Add a **Railway volume mounted at `/data`** to preserve the SQLite history across deployments. Keep one service replica to avoid SQLite collision and duplicate report races.
6. Deploy. Use `/matchimport` with `game.example.json` to test (its names and figures are **fictional examples**, not real PlayCHL results).
7. Once you obtain a verified per-game feed/export endpoint, set `GAME_FEED_URL`, then run `/matchrefresh` or let polling work automatically.

### Expected JSON format

Each game is an object with `id`, `status` (one of `final`, `final/ot`, `final/so`), `home:{name,score}`, `away:{name,score}`, optional `date`, `source_url`, `forfeit`, `team_stats:{home:{...},away:{...}}`, and `players:[{name,team,position,goals,assists,points,plus_minus,shots,hits,penalty_minutes,giveaways,takeaways,faceoffs_won,faceoffs_taken,saves,shots_against,goals_against,save_pct}]`. Unavailable fields may be omitted. **Player `team` strings must match the corresponding `home.name` or `away.name`.** See `game.example.json`.

For a polling feed, send a JSON array of games, or `{ "games": [ ... ] }`. Repeated games with the same `id` will not be reposted. The polling URL must use HTTPS; a token may be supplied with the `Authorization: Bearer` header.

## Local checks

```bash
pip install -r requirements.txt
PYTHONPATH=. python -m unittest discover -s tests -v
```

Known boundaries: automatic full-box-score ingestion is **not active** without a verified feed; reports reflect only supplied data, and stars are heuristic. Forfeits display no stars. The bot does not modify your other PlayCHL repositories or Railway services.
