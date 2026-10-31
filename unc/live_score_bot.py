#!/usr/bin/env python3
"""
TarHeel Tales live-score bot — every North Carolina (UNC) varsity sport.

Port of the Tiger Tales bot. Same wiki pages, same templates, same safety
rules; only the data source changes.

Data:
  goheels.com scoreboard API  /api/v2.1/EventsResults/scoreboard  (all sports:
      schedule, results, home/away, opponent rankings) and
      /api/v2/game-center/games (live games, when goheels exposes them)
  ESPN public scoreboard      live score / period for the team sports
  CollegeFootballData (CFBD)  optional, football down-and-distance + possession

Writes:
  Template:Live Score/Data   the Main Page card: live game > recent final > next game
  Template:Live Ticker/Data  the scrolling bar at the top of every page
  Template:Live Week/Data    every game in the next 7 days

Subcommands
  update   Hourly. Refresh all pages. If a game is live or starts within the
           hour, keep polling every POLL_SECONDS until nothing is live.
  render   Print the pages for a fixture file (testing, no network).

Environment
  WIKI_API_URL, WIKI_BOT_USER, WIKI_BOT_PASSWORD   wiki login (BotPassword)
  WIKI_BOT_UA        private user agent for wiki requests (keep it secret; never commit it)
  CFBD_API_KEY       optional, adds football situation/possession
  POLL_SECONDS       default 120
  DRY_RUN=1          print instead of editing the wiki
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TEAM = "North Carolina"          # name on the card
TICKER_TEAM = "UNC"              # name in the scrolling ticker
SITE = "https://goheels.com"
SCOREBOARD = SITE + "/api/v2.1/EventsResults/scoreboard"
LIVE_GAMES = SITE + "/api/v2/game-center/games"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
CFBD_BASE = "https://apinext.collegefootballdata.com"
EASTERN = ZoneInfo("America/New_York")

CARD_PAGE = os.environ.get("WIKI_CARD_PAGE", "Template:Live Score/Data")
TICKER_PAGE = os.environ.get("WIKI_TICKER_PAGE", "Template:Live Ticker/Data")
WEEK_PAGE = os.environ.get("WIKI_WEEK_PAGE", "Template:Live Week/Data")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"

FINAL_ON_CARD = timedelta(hours=4)      # keep a final on the card this long
FINALS_ON_TICKER = timedelta(days=5)    # recent results shown in the ticker
UPCOMING_ON_TICKER = timedelta(days=7)
LOOKAHEAD = timedelta(minutes=70)       # stay running if a game starts this soon
# Each run stops before the next hourly start (:07) so runs never queue up.
JOB_BUDGET = timedelta(minutes=int(os.environ.get("JOB_MINUTES", "52")))

# When several games are live at once, the card shows the highest priority.
PRIORITY = ["Football", "Men's Basketball", "Women's Basketball", "Baseball",
            "Women's Soccer", "Men's Soccer", "Field Hockey", "Men's Lacrosse",
            "Women's Lacrosse", "Softball", "Volleyball", "Wrestling", "Gymnastics"]
SHORT = {"Men's Basketball": "MBB", "Women's Basketball": "WBB", "Men's Soccer": "MSOC",
         "Women's Soccer": "WSOC", "Men's Tennis": "MTEN", "Women's Tennis": "WTEN",
         "Men's Golf": "MGOLF", "Women's Golf": "WGOLF", "Cross Country": "XC",
         "Track & Field": "T&F", "Track and Field": "T&F", "Football": "FB", "Baseball": "BASE",
         "Softball": "SB", "Volleyball": "VB", "Men's Lacrosse": "MLAX", "Women's Lacrosse": "WLAX",
         "Field Hockey": "FH", "Gymnastics": "GYM", "Rowing": "ROW", "Wrestling": "WRES",
         "Fencing": "FENC", "Swimming & Diving": "SWIM", "Swimming and Diving": "SWIM"}
# Sports where events are meets/tournaments rather than one opponent.
INDIVIDUAL = {"Men's Golf", "Women's Golf", "Men's Tennis", "Women's Tennis", "Cross Country",
              "Track & Field", "Track and Field", "Rowing", "Fencing", "Swimming & Diving",
              "Swimming and Diving", "Gymnastics"}

# ESPN scoreboard paths for live score/period (team sports only).
ESPN_PATHS = {
    "Football": "football/college-football",
    "Men's Basketball": "basketball/mens-college-basketball",
    "Women's Basketball": "basketball/womens-college-basketball",
    "Baseball": "baseball/college-baseball",
    "Softball": "baseball/college-softball",
    "Volleyball": "volleyball/womens-college-volleyball",
    "Men's Soccer": "soccer/usa.ncaa.m.1",
    "Women's Soccer": "soccer/usa.ncaa.w.1",
    "Men's Lacrosse": "lacrosse/mens-college-lacrosse",
    "Women's Lacrosse": "lacrosse/womens-college-lacrosse",
    "Field Hockey": "field-hockey/womens-college-field-hockey",
}

# Plain public user agent for third-party feeds (goheels.com, ESPN, CFBD only).
USER_AGENT = "TarHeelTalesLiveScoreBot/1.0 (https://tarheeltalesunc.com; User:Parkerfoodman)"
# Private user agent for requests to the wiki (Cloudflare exception). From the
# environment only. Do not post it in Discord, tickets or commits.
WIKI_USER_AGENT = os.environ.get("WIKI_BOT_UA", "").strip()


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {msg}", flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(s: str) -> datetime:
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- data

class Athletics:
    """goheels.com scoreboard API (all sports)."""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def scoreboard(self) -> list[dict]:
        r = self.s.get(SCOREBOARD, params={"pageSize": "60", "pageIndex": "0"}, timeout=30)
        r.raise_for_status()
        return r.json().get("items", []) or []

    def live_games(self) -> dict:
        """goheels game-center live games keyed by game id. Empty when none (HTTP 204)."""
        try:
            r = self.s.get(LIVE_GAMES, timeout=20)
            if r.status_code == 204 or not r.content:
                return {}
            r.raise_for_status()
            out = {}
            for g in r.json() or []:
                gid = g.get("gameId") or g.get("id") or g.get("game_id")
                if gid is not None:
                    out[int(gid)] = g
            if out:
                log(f"game-center live sample: {json.dumps(next(iter(out.values())))[:400]}")
            return out
        except Exception as e:
            log(f"game-center error (ignored): {e}")
            return {}


class EspnLive:
    """Live score/period from ESPN's public scoreboard, matched by date and team."""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT})
        self.cache: dict[tuple, list] = {}

    def events(self, sport: str, day: datetime) -> list:
        path = ESPN_PATHS.get(sport)
        if not path:
            return []
        key = (path, day.strftime("%Y%m%d"))
        if key not in self.cache:
            try:
                r = self.s.get(ESPN.format(path=path),
                               params={"dates": key[1], "limit": "500", "groups": "50"}, timeout=20)
                r.raise_for_status()
                self.cache[key] = r.json().get("events", []) or []
            except Exception as e:
                log(f"ESPN {sport} error (ignored): {e}")
                self.cache[key] = []
        return self.cache[key]

    @staticmethod
    def _is_us(c: dict) -> bool:
        t = c.get("team") or {}
        return t.get("location") == "North Carolina" or t.get("displayName") == "North Carolina Tar Heels"

    def find(self, g: dict) -> dict | None:
        for ev in self.events(g["sport"], g["start"].astimezone(EASTERN)):
            comp = (ev.get("competitions") or [{}])[0]
            teams = comp.get("competitors") or []
            us = [c for c in teams if self._is_us(c)]
            if not us:
                continue
            them = [c for c in teams if c is not us[0]]
            st = comp.get("status") or ev.get("status") or {}
            typ = st.get("type") or {}
            return {"state": typ.get("state"), "us": num(us[0].get("score")),
                    "them": num(them[0].get("score")) if them else "",
                    "period": typ.get("shortDetail") or ""}
        return None


class CFBD:
    def __init__(self, key: str):
        self.key = key
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT})

    def team_live(self) -> dict | None:
        if not self.key:
            return None
        try:
            r = self.s.get(CFBD_BASE + "/scoreboard", params={"classification": "fbs"}, timeout=30)
            r.raise_for_status()
        except Exception as e:
            log(f"CFBD error (ignored): {e}")
            return None
        for g in r.json():
            names = {(g.get("homeTeam") or {}).get("name"), (g.get("awayTeam") or {}).get("name")}
            if TEAM in names:
                return g
        return None


# --------------------------------------------------------------------------- model

def num(v) -> str:
    if v in (None, ""):
        return ""
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else f"{f:g}"
    except (TypeError, ValueError):
        return str(v)


RANK = re.compile(r"^\s*(#\d+|No\.\s*\d+)\s+")


def split_rank(name: str) -> tuple[str, str]:
    m = RANK.match(name or "")
    if not m:
        return (name or "").strip(), ""
    return name[m.end():].strip(), m.group(1).replace("No. ", "#").replace("No.", "#")


def tidy_text(t) -> str:
    """'5TH of 14' -> '5th of 14'; drop placeholders like '--' or 'COMPLETED'."""
    t = re.sub(r"\s+", " ", (t or "").replace(" | ", "; ")).strip()
    if t.lower() in ("", "--", "-", "completed", "final", "tba"):
        return ""
    t = re.sub(r"(\d)(ST|ND|RD|TH)\b", lambda m: m.group(1) + m.group(2).lower(), t)
    return re.sub(r"\bOF\b", "of", t)


LIVE_STATES = {"LIVE", "INPROGRESS", "IN_PROGRESS", "HALFTIME", "INTERMISSION", "DELAYED", "STARTED"}
OFF_STATES = {"POSTPONED", "CANCELED", "CANCELLED", "SUSPENDED"}


def _result_letter(us: str, them: str) -> str:
    if not (us.isdigit() and them.isdigit()):
        return ""
    return "W" if int(us) > int(them) else "L" if int(us) < int(them) else "T"


def normalize(e: dict, live: dict, espn: "EspnLive | None" = None, now: datetime | None = None) -> dict:
    """Flatten one goheels scoreboard item into what the card/ticker need."""
    sport = (e.get("sport") or {}).get("title") or "North Carolina"
    raw_opp = ((e.get("opponent") or {}).get("title") or e.get("tournament")
               or e.get("eventName") or "TBA")
    opp, opp_rank = split_rank(raw_opp)
    ha = {"H": "home", "A": "away", "N": "neutral"}.get(e.get("locationIndicator"), "home")
    res = e.get("result") or {}
    state = (e.get("gameStateDisplay") or "").upper().replace(" ", "")
    tbd = bool(e.get("tbd"))
    g = {
        "id": e["id"],
        "sport": sport,
        "opp": opp,
        "ha": ha,
        "start": parse_time(e.get("dateUtc") or e.get("date")),
        "tba": "date_tba" if tbd and not e.get("time") else (tbd or bool(e.get("allDay"))),
        "location": (e.get("location") or "").strip(),
        "rank": "",
        "opp_rank": opp_rank,
        "status": "upcoming",
        "us": "", "them": "", "result": "", "text": "", "period": "", "clock": "",
    }
    if state in OFF_STATES:
        g.update(status="off", text=state.title())
        return g
    lg = live.get(int(e["id"])) if live else None
    letter = (res.get("status") or "").upper()
    if state in LIVE_STATES or lg:
        g.update(status="live", us=num(res.get("teamScore")), them=num(res.get("opponentScore")))
        if lg:
            g["us"] = num(lg.get("teamScore") or lg.get("homeScore") or g["us"])
            g["them"] = num(lg.get("opponentScore") or lg.get("awayScore") or g["them"])
            g["period"] = str(lg.get("periodLabel") or lg.get("period") or "")
            g["clock"] = str(lg.get("clock") or lg.get("gameClock") or "")
    elif letter in ("W", "L", "T") and res.get("teamScore") not in (None, ""):
        g.update(status="final", us=num(res.get("teamScore")), them=num(res.get("opponentScore")),
                 result=letter, period=tidy_text(res.get("postScore")))
    elif letter or res.get("preScore") or res.get("postScore") or state == "GAMECOMPLETE":
        txt = tidy_text(" ".join(x for x in (res.get("preScore"), res.get("postScore")) if x))
        g.update(status="final" if txt else "done", text=txt)
    now = now or now_utc()
    # goheels lists games in its game center during pre-game coverage. Until
    # kickoff (or goheels' own live state), that's still an upcoming game.
    if g["status"] == "live" and state not in LIVE_STATES and g["start"] and now < g["start"]:
        g.update(status="upcoming", us="", them="", period="", clock="")
    # ESPN supplies the live score/period for team sports, and catches games
    # goheels hasn't flipped to live yet.
    if espn and g["sport"] in ESPN_PATHS and g["status"] in ("upcoming", "live") \
            and g["start"] - timedelta(minutes=20) <= now <= g["start"] + timedelta(hours=6):
        x = espn.find(g)
        if x and x["state"] == "pre" and state not in LIVE_STATES:
            g.update(status="upcoming", us="", them="", period="", clock="")
        elif x and x["state"] == "in":
            g.update(status="live", us=x["us"] if x["us"] not in (None, "") else g["us"],
                     them=x["them"] if x["them"] not in (None, "") else g["them"],
                     period=x["period"] or g["period"], clock="")
        elif x and x["state"] == "post":
            g.update(status="final", us=x["us"], them=x["them"], result=_result_letter(x["us"], x["them"]))
    return g


def per_sport(games: list[dict], n: int) -> list[dict]:
    seen: dict[str, int] = {}
    out = []
    for g in games:
        seen[g["sport"]] = seen.get(g["sport"], 0) + 1
        if seen[g["sport"]] <= n:
            out.append(g)
    return out


def vs_at(g: dict) -> str:
    return "at" if g["ha"] == "away" else "vs"


def matchup(g: dict) -> str:
    """'vs #15 Stanford', 'at Duke', or just the meet name for golf/tennis/XC."""
    name = (f"{g['opp_rank']} " if g["opp_rank"] else "") + g["opp"]
    return name if g["sport"] in INDIVIDUAL else f"{vs_at(g)} {name}"


def when(g: dict) -> str:
    dt = g["start"].astimezone(EASTERN)
    day = f"{dt:%a}, {dt:%b} {dt.day}"
    if g["tba"] == "date_tba":
        return "Date TBA"
    if g["tba"] or dt.strftime("%H:%M") in ("00:00", "00:01"):
        return day
    hour = dt.strftime("%I").lstrip("0")
    return f"{day} · {hour}:{dt:%M %p} ET"


# --------------------------------------------------------------------------- wikitext

def clean(v) -> str:
    return str(v).replace("|", "{{!}}").replace("\n", " ").replace("}}", "} }").strip()


def stamp() -> str:
    return datetime.now(EASTERN).strftime("%b %d, %I:%M %p ET").replace(" 0", " ")


def card_text(g: dict | None, football: dict | None = None) -> str:
    head = "<!-- Written automatically by the live-score bot. Manual edits will be overwritten. -->\n"
    if not g:
        return head + "{{Live Score/Box|status=none|updated=%s}}\n" % stamp()
    home_is_us = g["ha"] != "away"
    us_name = TEAM + (f" ({g['rank']})" if g["rank"] else "")
    them_name = (f"{g['opp_rank']} " if g["opp_rank"] else "") + g["opp"]
    f = {
        "status": "final" if g["status"] in ("final", "off") else g["status"],
        "sport": g["sport"],
        "game_id": g["id"],
        "home": us_name if home_is_us else them_name,
        "away": them_name if home_is_us else us_name,
        "home_score": (g["us"] if home_is_us else g["them"]) if g["status"] in ("live", "final") else "",
        "away_score": (g["them"] if home_is_us else g["us"]) if g["status"] in ("live", "final") else "",
        "result": g["result"],
        "note": g["text"],
        "period": g["period"],
        "clock": g["clock"],
        "venue": g["location"],
        "kickoff": when(g),
        "updated": stamp(),
    }
    if football and g["sport"] == "Football" and g["status"] == "live":
        poss = football.get("possession")
        teams = {"home": (football.get("homeTeam") or {}).get("name"),
                 "away": (football.get("awayTeam") or {}).get("name")}
        if poss in teams and teams[poss]:
            f["possession"] = f["home"] if (teams[poss] == TEAM) == home_is_us else f["away"]
        f["situation"] = football.get("situation") or ""
        f["tv"] = football.get("tv") or ""
        if football.get("clock"):
            f["clock"] = football["clock"]
        if football.get("period"):
            p = int(football["period"])
            f["period"] = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}.get(p, "OT")
    lines = [head + "{{Live Score/Box"]
    for k, v in f.items():
        if v not in (None, ""):
            lines.append(f"|{k}={clean(v)}")
    lines.append("}}")
    return "\n".join(lines) + "\n"


def ticker_item(g: dict) -> str:
    sport = SHORT.get(g["sport"], g["sport"])
    if g["status"] == "live":
        detail = " ".join(x for x in (g["period"], g["clock"]) if x)
        return (f"{{{{Live Ticker/Item|state=live|sport={clean(sport)}"
                f"|text={TICKER_TEAM} {g['us'] or 0}, {clean(g['opp'])} {g['them'] or 0}|detail={clean(detail)}}}}}")
    if g["status"] in ("final", "off"):
        if g["text"]:
            text = f"{clean(g['opp'])}: {clean(g['text'])}"
            res = ""
        else:
            text = f"{g['result'] + ' ' if g['result'] else ''}{g['us']}–{g['them']} {clean(matchup(g))}"
            res = g["result"]
        return f"{{{{Live Ticker/Item|state=final|sport={clean(sport)}|text={text}|result={res}}}}}"
    return (f"{{{{Live Ticker/Item|state=upcoming|sport={clean(sport)}"
            f"|text={clean(matchup(g))}|detail={clean(when(g))}}}}}")


def ticker_text(live: list, finals: list, upcoming: list) -> str:
    items = [ticker_item(g) for g in live + finals + upcoming]
    head = "<!-- Written automatically by the live-score bot. Manual edits will be overwritten. -->\n"
    if not items:
        return head + "<!-- no items -->\n"
    return head + "\n".join(items) + "\n<!-- updated %s -->\n" % stamp()


def week_text(games: list[dict], now: datetime) -> str:
    """Every game from now through the next 7 days, grouped by day (Eastern)."""
    head = "<!-- Written automatically by the live-score bot. Manual edits will be overwritten. -->\n"
    rows = sorted([g for g in games
                   if (g["status"] == "live" or (g["status"] == "upcoming" and g["start"] >= now - timedelta(hours=3)))
                   and g["start"] - now <= UPCOMING_ON_TICKER and g["tba"] != "date_tba"],
                  key=lambda g: (g["start"], g["sport"]))
    if not rows:
        return head + "{{Live Week/Empty}}\n"
    out, day = [head + "{{Live Week/Start}}"], None
    for g in rows:
        dt = g["start"].astimezone(EASTERN)
        label = "Today" if dt.date() == now.astimezone(EASTERN).date() else f"{dt:%A}, {dt:%b} {dt.day}"
        if label != day:
            out.append(f"{{{{Live Week/Day|{label}}}}}")
            day = label
        if g["status"] == "live":
            time_s = "LIVE"
        elif g["tba"] or dt.strftime("%H:%M") in ("00:00", "00:01"):
            time_s = "All day" if g["sport"] in INDIVIDUAL else "TBA"
        else:
            time_s = dt.strftime("%I:%M %p").lstrip("0")
        out.append(f"{{{{Live Week/Row|sport={clean(g['sport'])}|matchup={clean(matchup(g))}"
                   f"|time={time_s}|where={clean(g['location'])}|ha={g['ha']}}}}}")
    out.append("{{Live Week/End}}")
    return "\n".join(out) + "\n<!-- updated %s -->\n" % stamp()


def pick(games: list[dict], now: datetime):
    """Split into live / recent finals / upcoming, and choose the card game."""
    live = [g for g in games if g["status"] == "live"]
    finals = sorted([g for g in games if g["status"] in ("final", "off")
                     and now - g["start"] <= FINALS_ON_TICKER], key=lambda g: g["start"], reverse=True)
    upcoming = sorted([g for g in games if g["status"] == "upcoming" and g["start"] > now - timedelta(hours=6)
                       and g["start"] - now <= UPCOMING_ON_TICKER and g["tba"] != "date_tba"],
                      key=lambda g: g["start"])
    rank = lambda g: PRIORITY.index(g["sport"]) if g["sport"] in PRIORITY else len(PRIORITY)
    live.sort(key=rank)
    if live:
        card = live[0]
    else:
        recent = [g for g in finals if not g["text"] and now - g["start"] <= FINAL_ON_CARD + timedelta(hours=3)]
        future = [g for g in upcoming if g["start"] > now - timedelta(minutes=15)]
        card = recent[0] if recent else (future[0] if future else (finals[0] if finals else None))
    return card, live, per_sport(finals, 2)[:12], per_sport(upcoming, 2)[:10]


def needs_polling(games: list[dict], now: datetime) -> bool:
    if any(g["status"] == "live" for g in games):
        return True
    for g in games:
        if g["status"] == "upcoming" and not g["tba"]:
            start = g["start"]
            local = start.astimezone(EASTERN).strftime("%H:%M")
            if local in ("00:00", "00:01"):
                continue  # all-day / multi-day events (golf, tennis, XC)
            if now - timedelta(hours=4) <= start <= now + LOOKAHEAD:
                return True
    return False


# --------------------------------------------------------------------------- wiki

class Wiki:
    def __init__(self, api_url: str, user: str, password: str):
        self.api = api_url
        self.s = requests.Session()
        self.s.headers["User-Agent"] = WIKI_USER_AGENT
        self.user, self.password = user, password
        self.logged_in = False

    def _req(self, method, **p):
        p.update(format="json", formatversion="2")
        # Never follow redirects: a redirected POST silently becomes an empty GET
        # and the API answers with its HTML help page instead of JSON.
        r = (self.s.get(self.api, params=p, timeout=30, allow_redirects=False)
             if method == "GET"
             else self.s.post(self.api, data=p, timeout=30, allow_redirects=False))
        if 300 <= r.status_code < 400:
            raise RuntimeError(
                f"Wiki redirected ({r.status_code}) to {r.headers.get('Location')!r}. "
                f"Set WIKI_API_URL to that address (currently {self.api!r}).")
        if r.status_code >= 400:
            hint = (" Looks like the Cloudflare anti-bot challenge; check the User-Agent "
                    "exception." if "cloudflare" in r.text.lower() else "")
            raise RuntimeError(f"Wiki HTTP {r.status_code}: {r.text[:600]!r}{hint}")
        try:
            return r.json()
        except ValueError:
            raise RuntimeError(
                f"Wiki returned HTTP {r.status_code} with non-JSON content "
                f"({r.headers.get('Content-Type')!r}) from {self.api!r}. "
                f"Check WIKI_API_URL. Body starts: {r.text[:300]!r}") from None

    def login(self):
        if self.logged_in:
            return
        tok = self._req("GET", action="query", meta="tokens", type="login")["query"]["tokens"]["logintoken"]
        res = self._req("POST", action="login", lgname=self.user, lgpassword=self.password, lgtoken=tok)
        if res.get("login", {}).get("result") != "Success":
            raise RuntimeError(f"Wiki login failed: {res.get('login')}")
        self.logged_in = True

    def read(self, title: str) -> str:
        page = self._req("GET", action="query", prop="revisions", titles=title,
                         rvprop="content", rvslots="main")["query"]["pages"][0]
        return "" if page.get("missing") else page["revisions"][0]["slots"]["main"]["content"]

    def write(self, title: str, text: str, summary: str):
        self.login()
        csrf = self._req("GET", action="query", meta="tokens")["query"]["tokens"]["csrftoken"]
        res = self._req("POST", action="edit", title=title, text=text, summary=summary, bot="1", token=csrf)
        if res.get("edit", {}).get("result") != "Success":
            raise RuntimeError(f"Wiki edit failed: {res}")

    def purge(self, titles: str):
        try:
            self._req("POST", action="purge", titles=titles, forcelinkupdate="1")
        except Exception as e:
            log(f"Purge failed (non-fatal): {e}")


def make_wiki() -> Wiki | None:
    if DRY_RUN:
        return None
    url, user, pw = (os.environ.get(k) for k in ("WIKI_API_URL", "WIKI_BOT_USER", "WIKI_BOT_PASSWORD"))
    if not all((url, user, pw)):
        sys.exit("WIKI_API_URL, WIKI_BOT_USER and WIKI_BOT_PASSWORD must be set (or DRY_RUN=1)")
    if not WIKI_USER_AGENT:
        sys.exit("WIKI_BOT_UA must be set to the private wiki user agent (or DRY_RUN=1)")
    return Wiki(url, user, pw)


def comparable(t: str) -> str:
    """Ignore the timestamp lines when deciding whether an edit is needed."""
    return "\n".join(l for l in (t or "").splitlines()
                     if not l.startswith("|updated=") and not l.startswith("<!-- updated"))


class Publisher:
    def __init__(self, wiki: Wiki | None):
        self.wiki = wiki
        self.last: dict[str, str] = {}

    def put(self, title: str, text: str, summary: str) -> bool:
        if title not in self.last and self.wiki:
            self.last[title] = self.wiki.read(title)
        if comparable(self.last.get(title)) == comparable(text):
            return False
        if self.wiki is None:
            log(f"DRY RUN — {title}:\n{text}")
        else:
            self.wiki.write(title, text, summary)
            log(f"Wrote {title}: {summary}")
        self.last[title] = text
        return True


# --------------------------------------------------------------------------- commands

def gather(ath: Athletics, espn: EspnLive | None = None) -> list[dict]:
    live = ath.live_games()
    seen, games = set(), []
    for e in ath.scoreboard():
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        games.append(normalize(e, live, espn))
    return games


def summary_for(card: dict | None) -> str:
    if not card:
        return "No games scheduled"
    label = {"live": "Live", "final": "Final", "off": "Final", "upcoming": "Next"}.get(card["status"], "")
    return f"{label}: {card['sport']} {matchup(card)}"


def cmd_update(args):
    ath = Athletics()
    espn = EspnLive()
    cfbd = CFBD(os.environ.get("CFBD_API_KEY", ""))
    pub = Publisher(make_wiki())
    started = now_utc()
    failures = 0
    while True:
        now = now_utc()
        try:
            espn.cache.clear()
            games = gather(ath, espn)
            failures = 0
        except Exception as e:
            failures += 1
            log(f"Athletics feed error ({failures}/3): {e}")
            games = None
            if failures >= 3:
                log("Giving up for now; the next hourly run will try again.")
                sys.exit(1)
        if games is not None:
            card, live, finals, upcoming = pick(games, now)
            football = cfbd.team_live() if (card and card["sport"] == "Football"
                                                and card["status"] == "live") else None
            changed = pub.put(CARD_PAGE, card_text(card, football), summary_for(card))
            changed |= pub.put(TICKER_PAGE, ticker_text(live, finals, upcoming),
                               f"Ticker: {len(live)} live, {len(finals)} results, {len(upcoming)} upcoming")
            changed |= pub.put(WEEK_PAGE, week_text(games, now), "This week's games")
            if changed and pub.wiki:
                pub.wiki.purge("Main Page")
            if not needs_polling(games, now):
                log(f"Nothing live or about to start — done. ({len(games)} events checked)")
                return
            log(f"{len(live)} live; polling again in {POLL_SECONDS}s")
        if now_utc() - started > JOB_BUDGET:
            log("Hit time budget; the next hourly run will continue.")
            return
        time.sleep(POLL_SECONDS)


def cmd_render(args):
    fx = json.loads(Path(args.fixture).read_text())
    now = parse_time(fx["now"])
    live = {int(k): v for k, v in fx.get("live_games", {}).items()}
    games = [normalize(e, live, None, now) for e in fx["items"]]
    card, l, f, u = pick(games, now)
    print(card_text(card, fx.get("cfbd")))
    print(ticker_text(l, f, u))
    print(week_text(games, now))
    print("needs_polling:", needs_polling(games, now))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("update").set_defaults(fn=cmd_update)
    r = sub.add_parser("render"); r.add_argument("fixture"); r.set_defaults(fn=cmd_render)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
