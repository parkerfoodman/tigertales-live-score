# TarHeel Tales Live Score Bot

Live scores for every UNC varsity sport on [TarHeel Tales](https://tarheeltalesunc.com): the Main Page card, the scrolling score bar at the top of every page, and the "This week" list on Gameday. Port of the Tiger Tales bot; the wiki templates are identical.

**How it works:** GitHub Actions runs `live_score_bot.py update` hourly at :07. It reads goheels.com's scoreboard API (all sports), writes `Template:Live Score/Data`, `Template:Live Ticker/Data` and `Template:Live Week/Data` only when something changed, and purges the Main Page. If a game is live or starts within ~70 minutes it keeps polling every 2 minutes, stopping after ~52 minutes so runs never overlap; the next hourly run carries on.

**Live score sources:** goheels' own scoreboard and game-center feed, plus ESPN's public scoreboard for the live score/period in team sports (football, basketball, baseball, softball, soccer, volleyball, lacrosse, field hockey). With `CFBD_API_KEY` set, football also gets down-and-distance and possession.

## Secrets (Settings → Secrets and variables → Actions)
| Name | Value |
|---|---|
| `CFBD_API_KEY` | key from https://collegefootballdata.com/key (optional) |
| `WIKI_API_URL` | `https://tarheeltalesunc.com/w/api.php` once the domain is live (until then `https://tarheeltalesunc.miraheze.org/w/api.php`). Must be the final address: a redirect turns the login POST into a GET. |
| `WIKI_BOT_USER` | `Parkerfoodman@livescore` (from Special:BotPasswords on TarHeel Tales) |
| `WIKI_BOT_PASSWORD` | the bot password |
| `WIKI_BOT_UA` | the private User-Agent Miraheze whitelists in Cloudflare (see below). Never commit or post it. |

## Cloudflare exception (same as Tiger Tales' T16356)
Miraheze's Cloudflare blocks GitHub's servers. File a Phorge task asking for the bot's private User-Agent to be exempted for tarheeltalesunc.com's api.php (reference T16356, which Jenny_on_Wiki handled for Tiger Tales). Put the User-Agent only in the `WIKI_BOT_UA` secret and the private ticket.

## Wiki side
`Template:Live Score`, `Live Score/Box`, `Live Score/Data`, `Live Ticker/Item`, `Live Ticker/Data`, `Live Week/*`, the ticker block in `MediaWiki:Common.js`, and the `.tt-score`/`.tt-ticker` styles in `MediaWiki:Common.css`. **Turn off Approved Revs for the Template namespace** (Special:ManageWiki/namespaces/10), or the bot's edits sit in the queue and the cards go stale — same fix as Tiger Tales on Oct 23.

## Testing
- `python live_score_bot.py render tests/fixture.json` — sample live/final/upcoming output (no network). The fixture is a real goheels response from Oct 29, 2026 with women's soccer vs SMU set to live.
- `DRY_RUN=1 python live_score_bot.py update` — fetch for real, print instead of editing.
- Errors: `Wiki redirected (3xx)` → fix `WIKI_API_URL`; `Wiki HTTP 403` mentioning Cloudflare → User-Agent exception; `non-JSON content` → wrong URL.

## Good to know
- goheels' live game-center feed only answers during a live game; the first time it does, the log prints a sample (`game-center live sample:`) so the field names can be checked.
- GitHub pauses scheduled workflows after 60 days without commits; re-enable in the Actions tab in the offseason.
