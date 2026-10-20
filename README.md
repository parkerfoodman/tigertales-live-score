# Tiger Tales Live Score Bot

Puts a live Clemson football score card on the [Tiger Tales](https://tigertalesclemson.com) Main Page. It shows the next game during the week, the live score / quarter / clock / possession during the game, and the final afterward.

**How it works:** GitHub Actions runs `live_score_bot.py` every hour. On non-gamedays it exits in seconds and makes **no** API calls. On gameday it waits until 10 minutes before kickoff, then polls the CollegeFootballData.com (CFBD) scoreboard every 2 minutes and edits `Template:Live Score/Data` on the wiki, but only when something changed. It stops once the game goes final.

**API budget:** CFBD's free tier is 1,000 calls/month. One game is about 110–130 calls; schedule refreshes are about 8/month. Don't set `POLL_SECONDS` below ~90 without upgrading.

## Secrets (Settings → Secrets and variables → Actions)
| Name | Value |
|---|---|
| `CFBD_API_KEY` | key from https://collegefootballdata.com/key |
| `WIKI_API_URL` | `https://tigertalesclemson.com/w/api.php` |
| `WIKI_BOT_USER` | `Parkerfoodman@livescore` (from Special:BotPasswords) |
| `WIKI_BOT_PASSWORD` | the bot password |

## Wiki side
`Template:Live Score`, `Template:Live Score/Box`, `Template:Live Score/Data`, and the `.tt-score` styles in `MediaWiki:Common.css`. The Main Page shows the card with `{{Live Score}}` in its Gameday section.

## Testing
- `python live_score_bot.py render tests/fixture.json` prints sample upcoming/live/final wikitext (no network).
- `DRY_RUN=1 CFBD_API_KEY=... python live_score_bot.py schedule` prints instead of editing the wiki.
- Actions → "Live score" → Run workflow during a Clemson game.

## Good to know
- GitHub's scheduled runs can start several minutes late, so the bot wakes about an hour before kickoff and sleeps inside the job.
- GitHub pauses scheduled workflows on repos with no activity for 60 days. If that happens in the offseason, re-enable them in the Actions tab.
- Kickoff times are often TBA early in the week. The Sunday refresh catches updates; run "Refresh schedule" manually if a time changes mid-week.
- Football only (CFBD is football-only).
