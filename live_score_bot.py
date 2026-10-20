#!/usr/bin/env python3
"""
Tiger Tales live-score bot.

Pulls Clemson football scores from the CollegeFootballData.com (CFBD) API and
writes them to a data page on the Tiger Tales wiki, which the Main Page shows
through {{Live Score}}.

Subcommands
  schedule   Fetch this season's Clemson schedule (2 API calls) into schedule.json,
             and post a "Next game" box if nothing is in progress. Run weekly.
  run        If a Clemson game is near kickoff or in progress, poll the scoreboard
             and update the wiki until the game is final. Otherwise exit at once,
             spending zero API calls. Run hourly.
  render     Print the wikitext for a sample/fixture game (testing, no network).

Environment variables
  CFBD_API_KEY        CollegeFootballData.com API key (free tier is fine)
  WIKI_API_URL        e.g. https://tigertalesclemson.com/w/api.php
  WIKI_BOT_USER       BotPassword login name, e.g. Parkerfoodman@livescore
  WIKI_BOT_PASSWORD   BotPassword secret
  WIKI_DATA_PAGE      page the bot writes (default: Template:Live Score/Data)
  WIKI_PURGE_PAGES    pages to purge after each edit, "|"-separated (default: Main Page)
  TEAM                default: Clemson
  POLL_SECONDS        seconds between scoreboard calls (default: 120)
  DRY_RUN=1           print what would be written instead of editing the wiki
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

CFBD_BASE = "https://apinext.collegefootballdata.com"
EASTERN = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
SCHEDULE_FILE = HERE / "schedule.json"

TEAM = os.environ.get("TEAM", "Clemson")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))
DATA_PAGE = os.environ.get("WIKI_DATA_PAGE", "Template:Live Score/Data")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

# Start polling this long before kickoff, give up this long after.
PRE_KICK = timedelta(minutes=10)
MAX_GAME = timedelta(hours=5, minutes=30)
# GitHub Actions jobs die at 6h; stop a bit before that.
JOB_BUDGET = timedelta(hours=5, minutes=45)

USER_AGENT = "TigerTalesLiveScoreBot/1.0 (https://tigertalesclemson.com; User:Parkerfoodman)"


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {msg}", flush=True)


# --------------------------------------------------------------------------- CFBD

class CFBD:
    def __init__(self, key: str):
        if not key:
            sys.exit("CFBD_API_KEY is not set")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {key}",
                               "Accept": "application/json",
                               "User-Agent": USER_AGENT})
        self.calls = 0

    def get(self, path: str, **params):
        self.calls += 1
        r = self.s.get(CFBD_BASE + path, params=params, timeout=30)
        if r.status_code == 429:
            raise RuntimeError("CFBD monthly/rate limit hit (429)")
        r.raise_for_status()
        return r.json()

    def season_games(self, year: int, team: str) -> list[dict]:
        games = []
        for season_type in ("regular", "postseason"):
            games += self.get("/games", year=year, team=team, seasonType=season_type)
        return games

    def scoreboard(self) -> list[dict]:
        return self.get("/scoreboard", classification="fbs")


def pick(d: dict, *keys, default=None):
    """First present key — tolerates camelCase (v2) and snake_case (v1) fields."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def team_name(side) -> str:
    if isinstance(side, dict):
        return pick(side, "name", "school", "team", default="")
    return side or ""


# --------------------------------------------------------------------------- wiki

class Wiki:
    def __init__(self, api_url: str, user: str, password: str):
        self.api = api_url
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.user, self.password = user, password
        self.logged_in = False

    def _get(self, **p):
        p.update(format="json", formatversion="2")
        r = self.s.get(self.api, params=p, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Wiki HTTP {r.status_code}: {r.text[:600]!r}")
        return r.json()

    def _post(self, **p):
        p.update(format="json", formatversion="2")
        r = self.s.post(self.api, data=p, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Wiki HTTP {r.status_code}: {r.text[:600]!r}")
        return r.json()

    def login(self):
        if self.logged_in:
            return
        tok = self._get(action="query", meta="tokens", type="login")["query"]["tokens"]["logintoken"]
        res = self._post(action="login", lgname=self.user, lgpassword=self.password, lgtoken=tok)
        if res.get("login", {}).get("result") != "Success":
            raise RuntimeError(f"Wiki login failed: {res}")
        self.logged_in = True

    def read(self, title: str) -> str:
        data = self._get(action="query", prop="revisions", titles=title,
                         rvprop="content", rvslots="main")
        page = data["query"]["pages"][0]
        if page.get("missing"):
            return ""
        return page["revisions"][0]["slots"]["main"]["content"]

    def write(self, title: str, text: str, summary: str):
        self.login()
        csrf = self._get(action="query", meta="tokens")["query"]["tokens"]["csrftoken"]
        res = self._post(action="edit", title=title, text=text, summary=summary,
                         bot="1", token=csrf)
        if res.get("edit", {}).get("result") != "Success":
            raise RuntimeError(f"Wiki edit failed: {res}")
        # Main Page is cached; purge so the new score shows up right away.
        purge = os.environ.get("WIKI_PURGE_PAGES", "Main Page")
        if purge:
            try:
                self._post(action="purge", titles=purge, forcelinkupdate="1")
            except Exception as e:
                log(f"Purge failed (non-fatal): {e}")
        return res["edit"]


def make_wiki() -> Wiki | None:
    if DRY_RUN:
        return None
    url, user, pw = (os.environ.get(k) for k in ("WIKI_API_URL", "WIKI_BOT_USER", "WIKI_BOT_PASSWORD"))
    if not all((url, user, pw)):
        sys.exit("WIKI_API_URL, WIKI_BOT_USER and WIKI_BOT_PASSWORD must be set (or DRY_RUN=1)")
    return Wiki(url, user, pw)


def publish(wiki: Wiki | None, text: str, summary: str, current: str | None = None):
    """Edit only when the box actually changed (ignoring the 'updated' stamp)."""
    def strip(t):
        return "\n".join(l for l in (t or "").splitlines() if not l.startswith("|updated="))
    if current is not None and strip(current) == strip(text):
        log("No change — skipping edit")
        return
    if wiki is None:
        log("DRY RUN — would write:\n" + text)
        return
    wiki.write(DATA_PAGE, text, summary)
    log(f"Wrote {DATA_PAGE}: {summary}")


# --------------------------------------------------------------------------- rendering

def fmt_kickoff(start_iso: str) -> str:
    dt = parse_time(start_iso).astimezone(EASTERN)
    hour = dt.strftime("%I").lstrip("0")
    return f"{dt:%a, %b} {dt.day} · {hour}:{dt:%M %p} ET"


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def period_label(period) -> str:
    try:
        p = int(period)
    except (TypeError, ValueError):
        return ""
    return {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}.get(p, "OT" if p == 5 else f"{p - 4}OT")


def box(**fields) -> str:
    fields["updated"] = datetime.now(EASTERN).strftime("%b %d, %I:%M %p ET").replace(" 0", " ")
    order = ["status", "game_id", "home", "away", "home_score", "away_score", "result",
             "period", "clock", "possession", "situation", "last_play", "tv", "venue",
             "kickoff", "updated"]
    lines = ["<!-- Written automatically by LiveScoreBot. Manual edits will be overwritten on gameday. -->",
             "{{Live Score/Box"]
    for k in order:
        v = fields.get(k)
        if v not in (None, ""):
            v = str(v).replace("|", "{{!}}").replace("\n", " ")
            lines.append(f"|{k}={v}")
    lines.append("}}")
    return "\n".join(lines)


def box_upcoming(g: dict) -> str:
    return box(status="upcoming", game_id=g["id"], home=g["home"], away=g["away"],
               venue=g.get("venue"), tv=g.get("tv"), kickoff=fmt_kickoff(g["start"]))


def box_from_scoreboard(sb: dict, g: dict) -> str:
    home, away = sb.get("homeTeam", {}), sb.get("awayTeam", {})
    status = (sb.get("status") or "").lower()
    hs, as_ = pick(home, "points", default=0), pick(away, "points", default=0)
    state = {"in_progress": "live", "completed": "final"}.get(status, "upcoming")
    result = ""
    if state == "final":
        ours, theirs = (hs, as_) if team_name(home) == TEAM else (as_, hs)
        result = "W" if (ours or 0) > (theirs or 0) else "L" if (ours or 0) < (theirs or 0) else "T"
    poss = sb.get("possession")
    poss_team = team_name(home) if poss == "home" else team_name(away) if poss == "away" else ""
    venue = sb.get("venue") or {}
    venue_str = venue.get("name") if isinstance(venue, dict) else venue
    last_play = sb.get("lastPlay") or ""
    if len(last_play) > 160:
        last_play = last_play[:157] + "…"
    return box(status=state, game_id=pick(sb, "id", default=g["id"]),
               home=team_name(home) or g["home"], away=team_name(away) or g["away"],
               home_score=hs if state != "upcoming" else "",
               away_score=as_ if state != "upcoming" else "",
               result=result,
               period=period_label(sb.get("period")) if state == "live" else "",
               clock=sb.get("clock") if state == "live" else "",
               possession=poss_team if state == "live" else "",
               situation=sb.get("situation") if state == "live" else "",
               last_play=last_play if state == "live" else "",
               tv=sb.get("tv") or g.get("tv"),
               venue=venue_str or g.get("venue"),
               kickoff=fmt_kickoff(g["start"]))


# --------------------------------------------------------------------------- schedule

def normalize_game(raw: dict) -> dict | None:
    start = pick(raw, "startDate", "start_date")
    if not start:
        return None
    return {
        "id": pick(raw, "id"),
        "start": start,
        "home": pick(raw, "homeTeam", "home_team"),
        "away": pick(raw, "awayTeam", "away_team"),
        "venue": pick(raw, "venue"),
        "completed": bool(pick(raw, "completed", default=False)),
    }


def load_schedule() -> list[dict]:
    if not SCHEDULE_FILE.exists():
        return []
    return json.loads(SCHEDULE_FILE.read_text())["games"]


def find_active(games: list[dict], now: datetime) -> dict | None:
    for g in sorted(games, key=lambda g: g["start"]):
        start = parse_time(g["start"])
        if start - PRE_KICK - timedelta(minutes=60) <= now <= start + MAX_GAME:
            return g
    return None


def next_game(games: list[dict], now: datetime) -> dict | None:
    future = [g for g in games if parse_time(g["start"]) > now]
    return min(future, key=lambda g: g["start"]) if future else None


def cmd_schedule(args):
    cfbd = CFBD(os.environ.get("CFBD_API_KEY", ""))
    now = datetime.now(timezone.utc)
    year = args.year or (now.year if now.month >= 2 else now.year - 1)
    games = [g for g in (normalize_game(r) for r in cfbd.season_games(year, TEAM)) if g]
    games.sort(key=lambda g: g["start"])
    SCHEDULE_FILE.write_text(json.dumps({"season": year, "team": TEAM,
                                         "fetched": now.isoformat(), "games": games}, indent=2))
    log(f"Saved {len(games)} games for {year} ({cfbd.calls} API calls)")

    if find_active(games, now):
        log("A game is in progress/near kickoff — leaving the live box alone")
        return
    ng = next_game(games, now)
    if ng:
        wiki = make_wiki()
        current = wiki.read(DATA_PAGE) if wiki else None
        if current and "|status=final" in current and parse_time(ng["start"]) - now > timedelta(days=6):
            log("Keeping last final score up; next game is more than 6 days out")
            return
        publish(wiki, box_upcoming(ng), f"Next game: {ng['away']} at {ng['home']}", current)


def cmd_run(args):
    now = datetime.now(timezone.utc)
    games = load_schedule()
    g = find_active(games, now)
    if not g:
        log("No Clemson game in the live window — nothing to do (0 API calls)")
        return

    wiki = make_wiki()
    current = wiki.read(DATA_PAGE) if wiki else None
    if current and "|status=final" in current and f"|game_id={g['id']}" in current:
        log("This game is already marked final on the wiki — done")
        return

    start = parse_time(g["start"])
    deadline = min(start + MAX_GAME, now + JOB_BUDGET)

    # Before kickoff: show the upcoming box and sleep without spending API calls.
    publish(wiki, box_upcoming(g), f"Gameday: {g['away']} at {g['home']}", current)
    wait = (start - PRE_KICK - datetime.now(timezone.utc)).total_seconds()
    if wait > 0:
        log(f"Kickoff {fmt_kickoff(g['start'])} — sleeping {int(wait // 60)} min")
        time.sleep(wait)

    cfbd = CFBD(os.environ.get("CFBD_API_KEY", ""))
    last_text = None
    misses = 0
    while datetime.now(timezone.utc) < deadline:
        try:
            board = cfbd.scoreboard()
        except Exception as e:  # network blip — keep going
            log(f"Scoreboard error: {e}")
            time.sleep(POLL_SECONDS)
            continue
        sb = next((x for x in board
                   if TEAM in (team_name(x.get("homeTeam")), team_name(x.get("awayTeam")))), None)
        if sb is None:
            misses += 1
            log(f"{TEAM} not on scoreboard yet (miss {misses})")
        else:
            text = box_from_scoreboard(sb, g)
            state = "final" if "|status=final" in text else "live" if "|status=live" in text else "pre"
            try:
                publish(wiki, text, f"Score update ({state})", last_text)
                last_text = text
            except Exception as e:
                log(f"Wiki edit error: {e}")
            if state == "final":
                log(f"Final. Used {cfbd.calls} API calls this run.")
                return
        time.sleep(POLL_SECONDS)
    log(f"Hit time budget; used {cfbd.calls} API calls. Next hourly run will pick up if needed.")


def cmd_render(args):
    fixture = json.loads(Path(args.fixture).read_text())
    g = fixture["schedule_game"]
    print(box_upcoming(g))
    print()
    for sb in fixture["scoreboard_states"]:
        print(box_from_scoreboard(sb, g))
        print()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("schedule"); s.add_argument("--year", type=int); s.set_defaults(fn=cmd_schedule)
    r = sub.add_parser("run"); r.set_defaults(fn=cmd_run)
    t = sub.add_parser("render"); t.add_argument("fixture"); t.set_defaults(fn=cmd_render)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
