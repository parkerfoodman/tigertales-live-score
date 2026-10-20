#!/usr/bin/env python3
"""
Tiger Tales live-score bot — every Clemson varsity sport.

Data: the public schedule/results feed behind clemsontigers.com (one feed for
all 18 programs, plus a live-states feed during games). Football games also
get down-and-distance / possession from CollegeFootballData.com when a
CFBD_API_KEY is set.

Writes two wiki pages:
  Template:Live Score/Data   the Main Page card: live game > recent final > next game
  Template:Live Ticker/Data  the scrolling bar at the top of every page
  Template:Live Week/Data    every game in the next 7 days, under the Main Page card

Subcommands
  update   Hourly. Refresh both pages. If a game is live or starts within the
           hour, keep polling every POLL_SECONDS until nothing is live.
  render   Print both pages for a fixture file (testing, no network).

Environment
  WIKI_API_URL, WIKI_BOT_USER, WIKI_BOT_PASSWORD   wiki login (BotPassword)
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

SITE = "https://clemsontigers.com"
FEED = SITE + "/website-api/schedule-events"
LIVE_FEED = FEED + "/live-states"
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
JOB_BUDGET = timedelta(hours=5, minutes=45)

# When several games are live at once, the card shows the highest priority.
PRIORITY = ["Football", "Men's Basketball", "Women's Basketball", "Baseball",
            "Softball", "Volleyball", "Men's Soccer", "Women's Soccer", "Lacrosse",
            "Gymnastics"]
SHORT = {"Men's Basketball": "MBB", "Women's Basketball": "WBB", "Men's Soccer": "MSOC",
         "Women's Soccer": "WSOC", "Men's Tennis": "MTEN", "Women's Tennis": "WTEN",
         "Men's Golf": "MGOLF", "Women's Golf": "WGOLF", "Cross Country": "XC",
         "Track & Field": "T&F", "Football": "FB", "Baseball": "BASE", "Softball": "SB",
         "Volleyball": "VB", "Lacrosse": "LAX", "Gymnastics": "GYM", "Rowing": "ROW"}

# Sports where events are meets/tournaments rather than one opponent.
INDIVIDUAL = {"Men's Golf", "Women's Golf", "Men's Tennis", "Women's Tennis", "Cross Country",
              "Track & Field", "Rowing"}

USER_AGENT = "TigerTalesLiveScoreBot/1.0 (https://tigertalesclemson.com; User:Parkerfoodman)"


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {msg}", flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- data

class Athletics:
    """clemsontigers.com schedule feed (all sports)."""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def _events(self, **filters) -> list[dict]:
        params = {"include": "schedule.sport,scheduleEventResult",
                  "filter[hide_from_schedule_ticker]": "false", "per_page": "40"}
        params.update(filters)
        r = self.s.get(FEED, params=params, timeout=30)
        r.raise_for_status()
        return r.json().get("data", [])

    def recent(self) -> list[dict]:
        return self._events(**{"filter[past]": "true", "sort": "-datetime"})

    def upcoming(self) -> list[dict]:
        return self._events(**{"filter[upcoming]": "true", "sort": "datetime"})

    def live_states(self) -> dict:
        r = self.s.get(LIVE_FEED, timeout=30)
        r.raise_for_status()
        out = {}
        for item in r.json().get("data", []) or []:
            eid = item.get("schedule_event_id") or item.get("id")
            if eid is not None:
                out[int(eid)] = item
        return out


class CFBD:
    def __init__(self, key: str):
        self.key = key
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT})

    def clemson_live(self) -> dict | None:
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
            if "Clemson" in names:
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


def sport_of(e: dict) -> str:
    return ((e.get("schedule") or {}).get("sport") or {}).get("name") or "Clemson"


def opp_of(e: dict) -> str:
    return e.get("opponent_name") or (e.get("opponent") or {}).get("name") or "TBA"


def is_live(e: dict, live: dict) -> bool:
    st = live.get(int(e["id"]))
    if not st:
        return False
    state = str(st.get("statbroadcast_state") or st.get("state") or st.get("status") or "").lower()
    if st.get("not_yet_live") is True or state in ("not_yet_live", "pre", "pregame", "scheduled"):
        return False
    if state in ("final", "completed", "post", "postgame", "ended"):
        return False
    return True


def normalize(e: dict, live: dict) -> dict:
    """Flatten one feed event into what the card/ticker need."""
    res = e.get("schedule_event_result") or {}
    st = live.get(int(e["id"])) or {}
    g = {
        "id": e["id"],
        "sport": sport_of(e),
        "opp": opp_of(e),
        "ha": e.get("venue_type") or "home",
        "start": parse_time(e["datetime"]),
        "tba": e.get("tba"),
        "location": (e.get("location") or "").replace(" | ", " · "),
        "rank": e.get("ranking") or "",
        "opp_rank": e.get("opponent_ranking") or "",
        "status": "upcoming",
        "us": "", "them": "", "result": "", "text": "", "period": "", "clock": "",
    }
    if is_live(e, live):
        g.update(status="live", us=num(st.get("our_score")), them=num(st.get("opponent_score")),
                 period=st.get("period_label") or "", clock=st.get("status_label") or "")
    elif e.get("status") == "completed" or res.get("result"):
        r = res.get("result")
        w, l = num(res.get("winning_score")), num(res.get("losing_score"))
        if r == "win":
            g.update(status="final", us=w, them=l, result="W")
        elif r == "loss":
            g.update(status="final", us=l, them=w, result="L")
        elif r == "tie":
            g.update(status="final", us=w, them=l, result="T")
        else:
            txt = tidy_text(res.get("text"))
            if txt:
                g.update(status="final", text=txt)
            else:
                g["status"] = "done"  # completed, nothing meaningful to show
    if e.get("status") in ("cancelled", "canceled", "postponed"):
        g.update(status="off", text=e.get("status").title())
    return g


def tidy_text(t) -> str:
    """'5TH of 14' -> '5th of 14'; drop placeholders like '--' or 'COMPLETED'."""
    t = re.sub(r"\s+", " ", (t or "").replace(" | ", "; ")).strip()
    if t.lower() in ("", "--", "-", "completed", "final", "tba"):
        return ""
    t = re.sub(r"(\d)(ST|ND|RD|TH)\b", lambda m: m.group(1) + m.group(2).lower(), t)
    return re.sub(r"\bOF\b", "of", t)


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
    """'vs SMU', 'at Virginia', or just the meet name for golf/tennis/XC."""
    return g["opp"] if g["sport"] in INDIVIDUAL else f"{vs_at(g)} {g['opp']}"


def when(g: dict) -> str:
    dt = g["start"].astimezone(EASTERN)
    day = f"{dt:%a}, {dt:%b} {dt.day}"
    if g["tba"] in ("date_tba",):
        return "Date TBA"
    if g["tba"] or g["start"].astimezone(EASTERN).strftime("%H:%M") in ("00:00", "00:01"):
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
    us_name = "Clemson" + (f" ({g['rank']})" if g["rank"] else "")
    them_name = g["opp"] + (f" ({g['opp_rank']})" if g["opp_rank"] else "")
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
            f["possession"] = f["home"] if (teams[poss] == "Clemson") == home_is_us else f["away"]
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
                f"|text=Clemson {g['us'] or 0}, {clean(g['opp'])} {g['them'] or 0}|detail={clean(detail)}}}}}")
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
        self.s.headers["User-Agent"] = USER_AGENT
        self.user, self.password = user, password
        self.logged_in = False

    def _req(self, method, **p):
        p.update(format="json", formatversion="2")
        r = (self.s.get(self.api, params=p, timeout=30) if method == "GET"
             else self.s.post(self.api, data=p, timeout=30))
        if r.status_code >= 400:
            raise RuntimeError(f"Wiki HTTP {r.status_code}: {r.text[:600]!r}")
        return r.json()

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

def gather(ath: Athletics) -> list[dict]:
    live = {}
    try:
        live = ath.live_states()
    except Exception as e:
        log(f"live-states error (ignored): {e}")
    seen, games = set(), []
    for e in ath.recent() + ath.upcoming():
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        games.append(normalize(e, live))
    return games


def summary_for(card: dict | None) -> str:
    if not card:
        return "No games scheduled"
    label = {"live": "Live", "final": "Final", "off": "Final", "upcoming": "Next"}.get(card["status"], "")
    return f"{label}: {card['sport']} {matchup(card)}"


def cmd_update(args):
    ath = Athletics()
    cfbd = CFBD(os.environ.get("CFBD_API_KEY", ""))
    pub = Publisher(make_wiki())
    started = now_utc()
    failures = 0
    while True:
        now = now_utc()
        try:
            games = gather(ath)
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
            football = cfbd.clemson_live() if (card and card["sport"] == "Football"
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
    live = {int(k): v for k, v in fx.get("live_states", {}).items()}
    games = [normalize(e, live) for e in fx["events"]]
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
