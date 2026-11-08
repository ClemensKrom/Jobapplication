#!/usr/bin/env python3
"""
JobRadar - a small local job search app.

Runs a web server on your own computer, pulls real vacancies from
four remote-job boards, Randstad's vacancy search, the career pages of companies on the
watchlist, and Jooble when its free key is configured, scores them
against your profile and shows them in the browser.

Python 3.8 or newer, standard library only. Start it with
"Start JobRadar.bat" or with:
    python server.py
"""

import json
import os
import re
import sys
import threading
import time
import webbrowser
import html as htmllib
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# When packed into JobRadar.exe, the page and the example watchlist travel inside the exe
# (BUNDLE_DIR) while the data files are created next to the exe (APP_DIR).
FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(APP_DIR, "jobradar_data.json")
CACHE_FILE = os.path.join(APP_DIR, "jobradar_cache.json")
INDEX_FILE = os.path.join(BUNDLE_DIR, "index.html")
PORTS = range(8765, 8780)

# An honest client name. No browser impersonation.
UA = "JobRadar/1.0 (personal job search tool running on the job seeker's own computer)"

DEFAULT_PROFILE = {
    "skills": ["AI video", "content creation", "performance marketing"],
    "keywords": ["AI video", "content creator", "video editor", "performance marketing", "social media",
                 "contentmaker", "videograaf", "online marketeer"],
    "exclude": [],
    "city": "Eindhoven",
    "country": "Netherlands",
    "radius_km": 40,
    "days": 30,
    "want_local": True,
    "want_remote": True,
}

SOURCE_ORDER = ["Jooble", "Agencies", "Watchlist", "Remotive", "We Work Remotely", "Jobicy", "RemoteOK"]
KEYWORD_SOURCES = {"Jooble"}  # already searched by term, keep every result

# ----------------------------------------------------------------------
# Persistent state
# ----------------------------------------------------------------------

LOCK = threading.RLock()
STATE = {
    "profile": dict(DEFAULT_PROFILE),
    "saved": {},     # id -> job dict (kept even when the source drops it)
    "applied": {},   # id -> iso timestamp
    "hidden": {},    # id -> iso timestamp
    "seen": {},      # id -> iso timestamp of first sighting
    "last": {"at": "", "jobs": [], "sources": {}},
}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_state():
    d = load_json(DATA_FILE, None)
    if not isinstance(d, dict):
        return
    with LOCK:
        for k in ("saved", "applied", "hidden", "seen", "last"):
            if isinstance(d.get(k), dict):
                STATE[k] = d[k]
        p = d.get("profile")
        if isinstance(p, dict):
            prof = dict(DEFAULT_PROFILE)
            prof.update(p)
            STATE["profile"] = prof


def persist():
    with LOCK:
        save_json(DATA_FILE, STATE)


# ----------------------------------------------------------------------
# HTTP fetching with a small on-disk cache (respects the boards' limits)
# ----------------------------------------------------------------------

CACHE = load_json(CACHE_FILE, {})
CACHE_LOCK = threading.Lock()


def cache_get(key, ttl):
    with CACHE_LOCK:
        e = CACHE.get(key)
        if e and time.time() - e.get("t", 0) < ttl:
            return e.get("v")
    return None


def cache_put(key, value):
    with CACHE_LOCK:
        CACHE[key] = {"t": time.time(), "v": value}
        try:
            save_json(CACHE_FILE, CACHE)
        except Exception:
            pass


def http_get(url, accept="*/*", timeout=30):
    """Returns (status, text). Never raises."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": accept,
        "Accept-Language": "en,nl;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        return 0, str(e)


def cached_get(url, ttl, accept="*/*"):
    v = cache_get(url, ttl)
    if v is not None:
        return 200, v, True
    status, text = http_get(url, accept)
    if status == 200 and text:
        cache_put(url, text)
    return status, text, False


# ----------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def strip_html(s, limit=2000):
    s = htmllib.unescape(TAG_RE.sub(" ", str(s or "")))
    s = WS_RE.sub(" ", s).strip()
    return s[:limit]


def clean(s):
    return WS_RE.sub(" ", htmllib.unescape(str(s or ""))).strip()


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def to_date(value):
    """Best-effort conversion of many date formats to YYYY-MM-DD."""
    if value is None or value == "":
        return ""
    try:
        if isinstance(value, (int, float)) or re.fullmatch(r"\d{9,14}", str(value)):
            v = int(value)
            if v > 10 ** 11:
                v //= 1000
            return datetime.fromtimestamp(v, timezone.utc).strftime("%Y-%m-%d")
        s = str(value)
        if re.match(r"\d{4}-\d{2}-\d{2}", s):
            return s[:10]
        return parsedate_to_datetime(s).strftime("%Y-%m-%d")
    except Exception:
        return ""


def age_days(date_str):
    if not date_str:
        return 999
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - d).days)
    except Exception:
        return 999


DUTCH_STRONG = ["medewerker", "vacature", "stagiair", "werkstudent", "bijbaan", "communicatie",
                "marketeer", "vormgever", "videograaf", "maker", "wij zoeken", "zoeken wij",
                "wat ga je doen", "wie ben jij", "wij bieden", "dienstverband", "sollicit"]
DUTCH_WEAK = ["jij bent", "je bent", "ervaring", "werkzaamheden", "uur per week", "functie",
              "collega", "bedrijf", "fulltime", "parttime", "salaris", "ons team", "jouw",
              " een ", " het ", " voor ", " met ", " wij ", " je "]


def detect_dutch(text):
    t = " " + re.sub(r"[^a-zà-ÿ ]+", " ", str(text or "").lower()) + " "
    pts = sum(2 for w in DUTCH_STRONG if w in t) + sum(1 for w in DUTCH_WEAK if w in t)
    return "nl" if pts >= 3 else ""


REGION_OK = ["worldwide", "anywhere", "global", "europe", "emea", "netherlands", "international",
             "remote", "eu ", "utc", "cet", "gmt", "all countries"]
REGION_BAD = ["usa", "united states", "us only", "u.s.", "north america", "canada", "uk only",
              "united kingdom only", "latin america", "australia", "india", "philippines", "mexico",
              "brazil", "apac", "us timezone", "us-based", "eastern time", "pacific time"]


def region_check(text):
    """True = open to someone in the Netherlands, False = restricted elsewhere, None = unknown."""
    t = str(text or "").lower().strip()
    if not t:
        return None
    if any(w in t for w in REGION_OK):
        return True
    if any(w in t for w in REGION_BAD):
        return False
    return None


def word_start(fragment, text):
    """True when the fragment appears at the start of a word, so 'marketing' matches
    'marketingmedewerker' but 'hr' does not match 'through'."""
    return re.search(r"\b" + re.escape(fragment), text) is not None


def phrase_hit(phrase, text):
    p = norm(phrase)
    if not p:
        return 0.0
    if word_start(p, text):
        return 1.0
    words = [w for w in p.split() if len(w) > 2]
    if len(words) > 1 and all(word_start(w, text) for w in words):
        return 0.5
    return 0.0


# Rough coordinates of Dutch places, enough to say how far a vacancy is from her city.
CITY_COORDS = {
    "eindhoven": (51.4416, 5.4697), "amsterdam": (52.3676, 4.9041), "rotterdam": (51.9244, 4.4777),
    "den haag": (52.0705, 4.3007), "the hague": (52.0705, 4.3007), "s gravenhage": (52.0705, 4.3007),
    "utrecht": (52.0907, 5.1214), "tilburg": (51.5555, 5.0913), "breda": (51.5719, 4.7683),
    "s hertogenbosch": (51.6978, 5.3037), "den bosch": (51.6978, 5.3037), "hertogenbosch": (51.6978, 5.3037),
    "helmond": (51.4793, 5.6570), "veldhoven": (51.4190, 5.4049), "best": (51.5076, 5.3912),
    "son en breugel": (51.5125, 5.4914), "son": (51.5125, 5.4914), "nuenen": (51.4700, 5.5500),
    "geldrop": (51.4211, 5.5592), "valkenswaard": (51.3500, 5.4600), "waalre": (51.3867, 5.4458),
    "oirschot": (51.5050, 5.3100), "boxtel": (51.5900, 5.3300), "oss": (51.7650, 5.5180), "uden": (51.6600, 5.6200),
    "veghel": (51.6150, 5.5450), "venlo": (51.3704, 6.1724), "venray": (51.5250, 5.9750), "weert": (51.2510, 5.7064),
    "roermond": (51.1942, 5.9875), "maastricht": (50.8514, 5.6910), "heerlen": (50.8882, 5.9795),
    "sittard": (51.0000, 5.8700), "nijmegen": (51.8126, 5.8372), "arnhem": (51.9851, 5.8987),
    "apeldoorn": (52.2112, 5.9699), "amersfoort": (52.1561, 5.3878), "hilversum": (52.2292, 5.1669),
    "almere": (52.3508, 5.2647), "haarlem": (52.3874, 4.6462), "leiden": (52.1601, 4.4970), "delft": (52.0116, 4.3571),
    "dordrecht": (51.8133, 4.6901), "gouda": (52.0115, 4.7105), "zoetermeer": (52.0607, 4.4940),
    "groningen": (53.2194, 6.5665), "leeuwarden": (53.2012, 5.7999), "zwolle": (52.5168, 6.0830),
    "enschede": (52.2215, 6.8937), "deventer": (52.2551, 6.1639), "hengelo": (52.2659, 6.7931),
    "emmen": (52.7850, 6.8976), "assen": (52.9929, 6.5642), "alkmaar": (52.6324, 4.7534), "zaandam": (52.4420, 4.8292),
    "hoofddorp": (52.3061, 4.6907), "amstelveen": (52.3114, 4.8701), "schiphol": (52.3105, 4.7683),
    "roosendaal": (51.5308, 4.4653), "bergen op zoom": (51.4950, 4.2871), "waalwijk": (51.6826, 5.0729),
    "goes": (51.5040, 3.8900), "middelburg": (51.4988, 3.6136), "vlissingen": (51.4536, 3.5709),
    "ede": (52.0402, 5.6649), "veenendaal": (52.0286, 5.5589), "lelystad": (52.5185, 5.4714),
    "eersel": (51.3600, 5.3200), "bladel": (51.3700, 5.2200), "reusel": (51.3600, 5.1600), "budel": (51.2750, 5.5750),
    "budel dorplein": (51.2600, 5.5900), "maarheeze": (51.3100, 5.6200), "heeze": (51.3800, 5.5700),
    "someren": (51.3850, 5.7100), "asten": (51.4050, 5.7450), "deurne": (51.4600, 5.7950), "gemert": (51.5550, 5.6900),
    "beek en donk": (51.5300, 5.6300), "schijndel": (51.6200, 5.4300), "sint michielsgestel": (51.6400, 5.3500),
    "vught": (51.6500, 5.2900), "boxmeer": (51.6450, 5.9470), "cuijk": (51.7300, 5.8800), "heesch": (51.7350, 5.5300),
    "panningen": (51.3250, 5.9800), "sevenum": (51.4100, 6.0100), "horst": (51.4530, 6.0510), "roggel": (51.2700, 5.9300),
    "drunen": (51.6850, 5.1400), "nieuwkuijk": (51.6800, 5.1900), "westerhoven": (51.3420, 5.3980),
    "oisterwijk": (51.5800, 5.1900), "goirle": (51.5200, 5.0650), "dongen": (51.6260, 4.9380), "etten leur": (51.5700, 4.6360),
    "oosterhout": (51.6420, 4.8590), "zundert": (51.4710, 4.6560), "molenschot": (51.5900, 4.9100), "america": (51.4300, 5.9800),
    "cranendonck": (51.2900, 5.6000), "laarbeek": (51.5300, 5.6300), "bernheze": (51.7350, 5.5300), "meierijstad": (51.6150, 5.5450),
}
_CITY_KEYS = sorted(CITY_COORDS, key=len, reverse=True)


def city_coords(text):
    """Coordinates of every known Dutch place named in a location string."""
    t = " " + norm(text) + " "
    out = []
    for name in _CITY_KEYS:
        if " " + name + " " in t:
            c = CITY_COORDS[name]
            if c not in out:
                out.append(c)
            t = t.replace(" " + name + " ", " ")
    return out


def distance_km(a, b):
    import math
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def job_distance(job, home):
    """Kilometres from her city to the nearest named place of a local job, or None."""
    if not home or job.get("mode") == "remote":
        return None
    places = city_coords(job.get("location", ""))
    if not places:
        return None
    return int(round(min(distance_km(home, p) for p in places)))


# Dutch and English ways of saying the same job. Keys and values are matched after norm().
SYNONYMS = {
    "content creator": ["contentmaker", "content maker", "contentcreator", "contentspecialist", "content specialist",
                        "contentmarketeer", "content marketeer", "content producer", "contentproducent"],
    "content creation": ["contentcreatie", "content creatie", "contentproductie", "content productie",
                         "contentmarketing", "content marketing"],
    "video editor": ["videograaf", "videoeditor", "videomaker", "video maker", "video producer", "videoproducer",
                     "filmmaker", "video editing", "videobewerking"],
    "ai video": ["generatieve video", "ai content", "generative video", "ai generated video"],
    "performance marketing": ["online marketeer", "online marketing", "performance marketeer", "paid media",
                              "paid social", "sea specialist", "sea marketeer", "campagnemanager", "campaign manager",
                              "growth marketeer", "growth marketing", "digital marketeer", "digital marketing"],
    "social media": ["social media specialist", "socialmedia", "social media manager", "social media marketeer",
                     "community manager"],
    "contentmaker": ["content creator", "content maker", "contentcreator"],
    "videograaf": ["video editor", "videomaker", "videographer"],
    "online marketeer": ["performance marketing", "online marketing", "digital marketeer", "digital marketing"],
}
WORD_SYNONYMS = {
    "marketing": ["marketeer", "marketingmedewerker", "marketingspecialist", "marketingcommunicatie"],
    "content": ["contentmaker", "contentcreatie", "contentspecialist", "contentmarketeer", "contentproducent"],
    "video": ["videograaf", "videomaker", "videoproducer", "videographer"],
    "social": ["socialmedia"],
    "creator": ["maker", "creatie"],
    "communications": ["communicatie", "communicatiemedewerker", "communicatieadviseur"],
    "communication": ["communicatie", "communicatiemedewerker", "communicatieadviseur"],
}


def variants(phrase):
    p = norm(phrase)
    return [p] + [norm(x) for x in SYNONYMS.get(p, [])]


def best_hit(phrase, text):
    """Best phrase_hit over the phrase and its synonyms. Returns (score, matched variant)."""
    best, bv = 0.0, norm(phrase)
    for v in variants(phrase):
        h = phrase_hit(v, text)
        if h > best:
            best, bv = h, v
    return best, bv


ENGLISH_OK = ["english speaking", "english-speaking", "no dutch required", "dutch not required", "dutch is not required",
              "dutch is a plus", "dutch is an advantage", "dutch is a bonus", "dutch is nice to have",
              "working language is english", "english is our working language", "english as working language",
              "english as the working language", "fluent in english", "fluent english", "english proficiency",
              "international team", "international environment", "(eng)", "(english)", "english speakers",
              "engelstalig", "engels is de voertaal", "work in english", "working in english", "dutch not needed",
              "dutch not necessary", "dutch is optional", "dutch optional", "english only", "communicate in english",
              "english-language", "english language environment"]
DUTCH_REQUIRED = ["nederlands vereist", "nederlandse taal", "beheersing van de nederlandse taal", "beheersing nederlands",
                  "nederlands in woord en geschrift", "je spreekt nederlands", "je spreekt vloeiend nederlands",
                  "vloeiend nederlands", "nederlandstalig", "goed nederlands", "uitstekend nederlands",
                  "dutch required", "dutch is required", "fluent in dutch", "fluent dutch", "dutch speaking",
                  "dutch-speaking", "native dutch", "dutch language skills", "proficiency in dutch",
                  "dutch and english", "nederlands en engels", "dutch language"]


def english_flag(text):
    """'dutch-required', 'english-ok' or ''. Dutch-required wins when both appear."""
    t = " " + re.sub(r"\s+", " ", str(text or "").lower()) + " "
    # "no dutch required" must not count as "dutch required": blank the negated forms first.
    t2 = re.sub(r"(no|not|without|geen)\s+(?:\w+\s+){0,2}dutch\s+(?:required|needed|necessary)|dutch\s+(?:is\s+)?(?:not\s+required|not\s+needed|not\s+necessary|optional|a\s+plus|an\s+advantage|a\s+bonus|nice\s+to\s+have)", " ", t)
    if any(p in t2 for p in DUTCH_REQUIRED):
        return "dutch-required"
    if any(p in t for p in ENGLISH_OK):
        return "english-ok"
    return ""


def score_job(job, profile):
    """0-100, with the arithmetic in job["breakdown"] so the page can show it on hover."""
    title = norm(job["title"])
    body = norm(job.get("desc", "") + " " + " ".join(job.get("tags", [])))
    state = {"pts": 0.0}
    reasons = []
    lines = []
    matched = set()

    def add(points, label):
        state["pts"] += points
        lines.append("%s: %+d" % (label, round(points)))

    def how(phrase, v, h):
        s = "'%s'" % phrase if v == norm(phrase) else "'%s' as '%s'" % (phrase, v)
        return s + ("" if h == 1 else ", words apart")

    for s in profile.get("skills", []):
        h, v = best_hit(s, title)
        if h:
            add(45 * h, "skill %s in title" % how(s, v, h))
            reasons.append(v)
            matched.update(variants(s))
            continue
        h, v = best_hit(s, body)
        if h:
            add(15 * h, "skill %s in description" % how(s, v, h))
            reasons.append(v)
            matched.update(variants(s))
    for k in profile.get("keywords", []):
        if any(x in matched for x in variants(k)):
            continue
        h, v = best_hit(k, title)
        if h:
            add(35 * h, "search term %s in title" % how(k, v, h))
            reasons.append(v)
            matched.update(variants(k))
            continue
        h, v = best_hit(k, body)
        if h:
            add(10 * h, "search term %s in description" % how(k, v, h))
            reasons.append(v)
            matched.update(variants(k))
    age = job.get("age_days", 999)
    if age <= 3:
        add(8, "posted in the last 3 days")
    elif age <= 7:
        add(4, "posted in the last week")
    if job.get("region_ok") is False:
        add(-40, "limited to %s" % (job.get("region") or "another region"))
    job["excluded"] = any(norm(x) and norm(x) in title for x in profile.get("exclude", []))
    job["score"] = int(max(0, min(100, round(state["pts"]))))
    job["reasons"] = reasons[:6]
    if not lines:
        lines.append("none of your skills or search terms found in title or description")
    lines.append("= %d%% (skills: 45 title / 15 description, search terms: 35 / 10, capped at 100)" % job["score"])
    job["breakdown"] = lines
    return job


def make_job(source, jid, title, company, location, url, posted, desc="", tags=None,
             mode="remote", employment="", region="", salary=""):
    job = {
        "id": "%s:%s" % (source.lower().replace(" ", ""), jid),
        "source": source,
        "title": clean(title),
        "company": clean(company) or "Unknown company",
        "location": clean(location),
        "url": url,
        "posted": to_date(posted),
        "desc": strip_html(desc),
        "tags": [clean(t) for t in (tags or []) if clean(t)][:20],
        "mode": mode,
        "employment": employment,
        "region": clean(region),
        "salary": clean(salary),
    }
    job["age_days"] = age_days(job["posted"])
    job["region_ok"] = region_check(job["region"]) if mode == "remote" else True
    job["lang"] = detect_dutch(job["title"] + " " + job["desc"])
    job["english"] = english_flag(job["title"] + " " + job["desc"])
    return job


# ----------------------------------------------------------------------
# Search progress
# ----------------------------------------------------------------------

SEARCH = {"running": False, "stop": False, "started": "", "sources": {}, "jobs": [], "message": ""}


def src_status(name, status, msg="", count=None):
    with LOCK:
        e = SEARCH["sources"].setdefault(name, {"status": "pending", "msg": "", "count": 0})
        e["status"] = status
        e["msg"] = msg
        if count is not None:
            e["count"] = count


def stopped():
    return SEARCH["stop"]


# ----------------------------------------------------------------------
# Optional keyed source for jobs near the city: Jooble.
# Official API with a free key. Put the key in config.json.
# ----------------------------------------------------------------------

CONFIG_FILE = os.path.join(APP_DIR, "config.json")
CONFIG_TEMPLATE = {
    "jooble_key": "",
    "_help": "Optional. Request a free Jooble key at https://jooble.org/api/about and paste it here. Leave empty to skip Jooble.",
}


def load_config():
    cfg = load_json(CONFIG_FILE, None)
    if not isinstance(cfg, dict):
        try:
            save_json(CONFIG_FILE, CONFIG_TEMPLATE)
        except Exception:
            pass
        return dict(CONFIG_TEMPLATE)
    out = dict(CONFIG_TEMPLATE)
    for k, v in cfg.items():
        if not k.startswith("_"):
            out[k] = str(v or "").strip()
    return out


def search_terms(profile):
    return [k for k in profile.get("keywords", []) if k.strip()][:8] or profile.get("skills", [])[:3]


def src_jooble(profile):
    name = "Jooble"
    cfg = load_config()
    if not profile.get("want_local", True):
        src_status(name, "off", "local search switched off", 0)
        return []
    key = cfg.get("jooble_key")
    if not key:
        src_status(name, "off", "no API key in config.json", 0)
        return []
    terms = search_terms(profile)
    jobs = {}
    src_status(name, "running", "0/%d searches" % len(terms), 0)
    for i, term in enumerate(terms):
        if stopped():
            break
        body = {"keywords": term, "location": profile.get("city", "Eindhoven"),
                "radius": str(int(profile.get("radius_km", 40))), "page": "1"}
        cache_key = "jooble:" + json.dumps(body, sort_keys=True)
        text = cache_get(cache_key, 3600)
        if text is None:
            req = urllib.request.Request(
                "https://jooble.org/api/" + urllib.parse.quote(key), data=json.dumps(body).encode("utf-8"),
                headers={"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"},
                method="POST")
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    text = r.read().decode("utf-8", "replace")
                cache_put(cache_key, text)
            except urllib.error.HTTPError as e:
                src_status(name, "running", "'%s' failed (HTTP %s)" % (term, e.code), len(jobs))
                continue
            except Exception as e:
                src_status(name, "running", "'%s' failed (%s)" % (term, e), len(jobs))
                continue
        try:
            data = json.loads(text)
        except Exception:
            continue
        for it in data.get("jobs", []):
            t = str(it.get("type") or "").lower()
            if "part" in t:
                employment = "part-time"
            elif "contract" in t or "freelance" in t or "temporary" in t:
                employment = "contract"
            elif "full" in t:
                employment = "full-time"
            else:
                employment = ""
            j = make_job(name, it.get("id"), it.get("title"), it.get("company"), it.get("location"),
                         it.get("link"), it.get("updated"), it.get("snippet"), [], mode="local",
                         employment=employment, salary=it.get("salary"))
            jobs[j["id"]] = j
        src_status(name, "running", "%d/%d searches" % (i + 1, len(terms)), len(jobs))
    src_status(name, "done", "%d searches" % len(terms), len(jobs))
    return list(jobs.values())


# ----------------------------------------------------------------------
# Source: Remotive (full feed, cached 12 h to respect their request)
# ----------------------------------------------------------------------

EMP_MAP = {"full_time": "full-time", "full-time": "full-time", "full time": "full-time",
           "contract": "contract", "freelance": "contract", "part_time": "part-time",
           "part-time": "part-time", "part time": "part-time", "internship": "internship"}


def emp(value):
    return EMP_MAP.get(str(value or "").strip().lower(), "")


def src_remotive(profile):
    name = "Remotive"
    src_status(name, "running", "fetching feed")
    status, text, cached = cached_get("https://remotive.com/api/remote-jobs", 12 * 3600, "application/json")
    if status != 200:
        src_status(name, "error", "feed unavailable (HTTP %s)" % status, 0)
        return []
    try:
        data = json.loads(text)
    except Exception:
        src_status(name, "error", "bad response", 0)
        return []
    jobs = []
    for it in data.get("jobs", []):
        jobs.append(make_job(name, it.get("id"), it.get("title"), it.get("company_name"),
                             it.get("candidate_required_location") or "Remote", it.get("url"),
                             it.get("publication_date"), it.get("description"), it.get("tags") or [],
                             mode="remote", employment=emp(it.get("job_type")),
                             region=it.get("candidate_required_location"), salary=it.get("salary")))
    src_status(name, "done", "feed %s" % ("(cached)" if cached else "fetched"), len(jobs))
    return jobs


# ----------------------------------------------------------------------
# Source: RemoteOK (full feed, cached 6 h)
# ----------------------------------------------------------------------

def src_remoteok(profile):
    name = "RemoteOK"
    src_status(name, "running", "fetching feed")
    status, text, cached = cached_get("https://remoteok.com/api", 6 * 3600, "application/json")
    if status != 200:
        src_status(name, "error", "feed unavailable (HTTP %s)" % status, 0)
        return []
    try:
        data = json.loads(text)
    except Exception:
        src_status(name, "error", "bad response", 0)
        return []
    jobs = []
    for it in data:
        if not isinstance(it, dict) or not it.get("position"):
            continue
        tags = [str(t) for t in (it.get("tags") or [])]
        employment = "contract" if "contract" in tags else "part-time" if "part-time" in tags else ""
        salary = ""
        try:
            smin, smax = int(it.get("salary_min") or 0), int(it.get("salary_max") or 0)
            if smin > 0:
                salary = "$%dk-%dk/yr" % (smin // 1000, smax // 1000)
        except Exception:
            pass
        jobs.append(make_job(name, it.get("id"), it.get("position"), it.get("company"),
                             it.get("location") or "Remote", it.get("url"), it.get("date"),
                             it.get("description"), tags, mode="remote", employment=employment,
                             region=it.get("location"), salary=salary))
    src_status(name, "done", "feed %s" % ("(cached)" if cached else "fetched"), len(jobs))
    return jobs


# ----------------------------------------------------------------------
# Source: Jobicy (tag searches, cached 1 h)
# ----------------------------------------------------------------------

def src_jobicy(profile):
    name = "Jobicy"
    tags = ["video", "marketing", "content", "social media"]
    jobs = {}
    src_status(name, "running", "0/%d tags" % len(tags))
    for i, tag in enumerate(tags):
        if stopped():
            break
        url = "https://jobicy.com/api/v2/remote-jobs?" + urllib.parse.urlencode({"count": 50, "tag": tag})
        status, text, cached = cached_get(url, 3600, "application/json")
        if status != 200:
            src_status(name, "running", "tag '%s' failed (HTTP %s)" % (tag, status), len(jobs))
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue
        for it in data.get("jobs", []):
            jt = it.get("jobType") or []
            employment = emp(jt[0]) if isinstance(jt, list) and jt else emp(jt)
            salary = ""
            if it.get("salaryMin"):
                salary = "%s %s-%s/%s" % (it.get("salaryCurrency", ""), it.get("salaryMin"), it.get("salaryMax"), it.get("salaryPeriod", "yr"))
            j = make_job(name, it.get("id"), it.get("jobTitle"), it.get("companyName"),
                         it.get("jobGeo") or "Remote", it.get("url"), it.get("pubDate"),
                         it.get("jobDescription") or it.get("jobExcerpt"), it.get("jobIndustry") or [],
                         mode="remote", employment=employment, region=it.get("jobGeo"), salary=salary)
            jobs[j["id"]] = j
        src_status(name, "running", "%d/%d tags" % (i + 1, len(tags)), len(jobs))
        if not cached:
            time.sleep(0.5)
    src_status(name, "done", "%d tags" % len(tags), len(jobs))
    return list(jobs.values())


# ----------------------------------------------------------------------
# Source: We Work Remotely (RSS, cached 6 h)
# ----------------------------------------------------------------------

WWR_FEEDS = [
    "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
    "https://weworkremotely.com/categories/remote-design-jobs.rss",
]


def src_wwr(profile):
    name = "We Work Remotely"
    jobs = {}
    src_status(name, "running", "0/%d feeds" % len(WWR_FEEDS))
    for i, feed in enumerate(WWR_FEEDS):
        if stopped():
            break
        status, text, cached = cached_get(feed, 6 * 3600, "application/rss+xml")
        if status != 200:
            src_status(name, "running", "feed %d failed (HTTP %s)" % (i + 1, status), len(jobs))
            continue
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except Exception:
            continue
        for item in root.iter("item"):
            g = lambda tag: (item.findtext(tag) or "").strip()
            full = g("title")
            company, _, title = full.partition(": ")
            if not title:
                title, company = full, ""
            skills = re.split(r",\s*|\s+and\s+", g("skills"))
            j = make_job(name, g("guid") or g("link"), title, company, g("region") or "Remote",
                         g("link"), g("pubDate"), g("description"), skills, mode="remote",
                         employment=emp(g("type")), region=g("region"))
            jobs[j["id"]] = j
        src_status(name, "running", "%d/%d feeds" % (i + 1, len(WWR_FEEDS)), len(jobs))
    src_status(name, "done", "%d feeds" % len(WWR_FEEDS), len(jobs))
    return list(jobs.values())




# ----------------------------------------------------------------------
# Company watchlist: the public career-page APIs of Recruitee, Greenhouse,
# Lever, Workable, Ashby and Personio. Add a company by pasting the URL of
# its careers page; JobRadar works out which system it uses.
# ----------------------------------------------------------------------

WATCH_FILE = os.path.join(APP_DIR, "watchlist.json")
WATCH_COUNTS = {}   # company id -> {"count": n, "err": "", "at": iso}

ATS_PATTERNS = [
    ("recruitee", re.compile(r"https?://([a-z0-9-]+)\.recruitee\.com", re.I)),
    ("recruitee", re.compile(r"([a-z0-9-]+)\.recruitee\.com/api", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/(?:embed/job_board(?:/js)?\?for=|v1/boards/)([a-z0-9-]+)", re.I)),
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([a-z0-9-]+)", re.I)),
    ("lever", re.compile(r"(?:jobs|api)\.lever\.co/(?:v0/postings/)?([a-z0-9-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/(?:api/v\d/widget/accounts/)?([a-z0-9-]+)", re.I)),
    ("workable", re.compile(r"https?://([a-z0-9-]+)\.workable\.com", re.I)),
    ("ashby", re.compile(r"(?:jobs\.ashbyhq\.com|api\.ashbyhq\.com/posting-api/job-board)/([a-z0-9-]+)", re.I)),
    ("personio", re.compile(r"https?://([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", re.I)),
    ("teamtailor", re.compile(r"https?://([a-z0-9-]+\.teamtailor\.com)", re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9-]+)", re.I)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9-]+)", re.I)),
]
SKIP_SLUGS = {"www", "apply", "boards", "job-boards", "api", "jobs", "embed", "app", "careers", "help", "docs", "static", "cdn"}
ATS_LABEL = {"recruitee": "Recruitee", "greenhouse": "Greenhouse", "lever": "Lever",
             "workable": "Workable", "ashby": "Ashby", "personio": "Personio",
             "teamtailor": "Teamtailor", "smartrecruiters": "SmartRecruiters"}
ATS_NAMES = "Recruitee, Greenhouse, Lever, Workable, Ashby, Personio, Teamtailor and SmartRecruiters"


def load_watchlist():
    if not os.path.exists(WATCH_FILE):
        # First start: copy the example list that ships with the app.
        seed = os.path.join(BUNDLE_DIR, "watchlist.json")
        if os.path.exists(seed) and seed != WATCH_FILE:
            try:
                save_json(WATCH_FILE, load_json(seed, {"companies": []}))
            except Exception:
                pass
    d = load_json(WATCH_FILE, None)
    return [c for c in d.get("companies", []) if isinstance(c, dict)] if isinstance(d, dict) else []


def save_watchlist(companies):
    save_json(WATCH_FILE, {"companies": companies})


def emp_any(*values):
    t = " ".join(str(v or "") for v in values).lower().replace("_", " ")
    if "intern" in t or "trainee" in t or "student" in t:
        return "internship"
    if "part" in t:
        return "part-time"
    if "full" in t or "permanent" in t:
        return "full-time"
    if "freelance" in t or "contract" in t or "temporary" in t:
        return "contract"
    return ""


def remote_mode(*values):
    t = " ".join(str(v or "") for v in values).lower()
    return "remote" if "remote" in t else "local"


def ats_fetch(ats, slug, name="", ttl=3600):
    """Returns (jobs, error). error is None on success."""
    src = "Watchlist"
    cname = name or slug.replace("-", " ").title()
    jobs = []
    try:
        if ats == "recruitee":
            st, text, _ = cached_get("https://%s.recruitee.com/api/offers/" % slug, ttl, "application/json")
            if st != 200:
                return [], "HTTP %s" % st
            offers = json.loads(text).get("offers", [])
            if not name and offers and offers[0].get("company_name"):
                cname = offers[0]["company_name"]
            for o in offers:
                loc = o.get("location") or ", ".join(x for x in [o.get("city"), o.get("country")] if x)
                jobs.append(make_job(src, "%s:%s:%s" % (ats, slug, o.get("id")), o.get("title"), cname, loc,
                                     o.get("careers_url"), o.get("published_at") or o.get("created_at"),
                                     o.get("description"), [o.get("category_code") or ""],
                                     mode="remote" if o.get("remote") else "local",
                                     employment=emp_any(o.get("employment_type_code"))))
        elif ats == "greenhouse":
            st, text, _ = cached_get("https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true" % slug, ttl, "application/json")
            if st != 200:
                return [], "HTTP %s" % st
            for o in json.loads(text).get("jobs", []):
                loc = (o.get("location") or {}).get("name", "")
                deps = [d.get("name", "") for d in (o.get("departments") or [])]
                jobs.append(make_job(src, "%s:%s:%s" % (ats, slug, o.get("id")), o.get("title"), cname, loc,
                                     o.get("absolute_url"), o.get("first_published") or o.get("updated_at"),
                                     htmllib.unescape(o.get("content") or ""), deps, mode=remote_mode(loc)))
        elif ats == "lever":
            st, text, _ = cached_get("https://api.lever.co/v0/postings/%s?mode=json" % slug, ttl, "application/json")
            if st != 200:
                return [], "HTTP %s" % st
            for o in json.loads(text):
                cat = o.get("categories") or {}
                loc = cat.get("location", "")
                jobs.append(make_job(src, "%s:%s:%s" % (ats, slug, o.get("id")), o.get("text"), cname, loc,
                                     o.get("hostedUrl"), o.get("createdAt"), o.get("descriptionPlain"),
                                     [cat.get("team") or ""], mode=remote_mode(loc, o.get("workplaceType")),
                                     employment=emp_any(cat.get("commitment"))))
        elif ats == "workable":
            st, text, _ = cached_get("https://apply.workable.com/api/v1/widget/accounts/%s" % slug, ttl, "application/json")
            if st != 200:
                return [], "HTTP %s" % st
            data = json.loads(text)
            if not name and data.get("name"):
                cname = data["name"]
            for o in data.get("jobs", []):
                loc = ", ".join(x for x in [o.get("city"), o.get("country")] if x)
                jobs.append(make_job(src, "%s:%s:%s" % (ats, slug, o.get("shortcode") or o.get("code")), o.get("title"),
                                     cname, loc, o.get("url"), o.get("published_on"), "", [o.get("department") or ""],
                                     mode="remote" if o.get("telecommuting") else "local",
                                     employment=emp_any(o.get("employment_type"))))
        elif ats == "ashby":
            st, text, _ = cached_get("https://api.ashbyhq.com/posting-api/job-board/%s" % slug, ttl, "application/json")
            if st != 200:
                return [], "HTTP %s" % st
            for o in json.loads(text).get("jobs", []):
                jobs.append(make_job(src, "%s:%s:%s" % (ats, slug, o.get("id")), o.get("title"), cname, o.get("location"),
                                     o.get("jobUrl"), o.get("publishedAt"), o.get("descriptionPlain"),
                                     [o.get("department") or ""], mode="remote" if o.get("isRemote") else "local",
                                     employment=emp_any(o.get("employmentType"))))
        elif ats == "personio":
            st, text, _ = cached_get("https://%s.jobs.personio.de/xml" % slug, ttl, "application/xml")
            if st != 200:
                return [], "HTTP %s" % st
            root = ET.fromstring(text.encode("utf-8"))
            for p in root.iter("position"):
                g = lambda tag: (p.findtext(tag) or "").strip()
                desc = " ".join((d.findtext("value") or "") for d in p.iter("jobDescription"))
                jobs.append(make_job(src, "%s:%s:%s" % (ats, slug, g("id")), g("name"), cname, g("office"),
                                     "https://%s.jobs.personio.de/job/%s" % (slug, g("id")), g("createdAt"), desc,
                                     [g("department"), g("recruitingCategory")], mode=remote_mode(g("office")),
                                     employment=emp_any(g("employmentType"), g("schedule"))))
        elif ats == "teamtailor":
            st, text, _ = cached_get("https://%s/jobs.json" % slug, ttl, "application/json")
            if st != 200:
                return [], "HTTP %s" % st
            data = json.loads(text)
            if not name and data.get("title"):
                cname = data["title"]
            for o in data.get("items", []):
                jp = o.get("_jobposting") or {}
                locs = jp.get("jobLocation") or []
                if isinstance(locs, dict):
                    locs = [locs]
                parts = []
                for l in locs:
                    a = (l or {}).get("address") or {}
                    s = ", ".join(x for x in [a.get("addressLocality"), a.get("addressCountry")] if x)
                    if s and s not in parts:
                        parts.append(s)
                loc = "; ".join(parts)
                remote = str(jp.get("jobLocationType") or "").upper() == "TELECOMMUTE"
                jobs.append(make_job(src, "%s:%s:%s" % (ats, slug, o.get("id")), o.get("title"), cname,
                                     (loc or "Remote") if remote else loc, o.get("url"), o.get("date_published"),
                                     o.get("content_html"), [], mode="remote" if remote else remote_mode(loc),
                                     employment=emp_any(jp.get("employmentType"))))
        elif ats == "smartrecruiters":
            st, text, _ = cached_get("https://api.smartrecruiters.com/v1/companies/%s/postings?limit=100" % slug, ttl, "application/json")
            if st != 200:
                return [], "HTTP %s" % st
            data = json.loads(text)
            for o in data.get("content", []):
                if not name and (o.get("company") or {}).get("name"):
                    cname = o["company"]["name"]
                l = o.get("location") or {}
                loc = ", ".join(x for x in [l.get("city"), str(l.get("country") or "").upper()] if x)
                tags = [(o.get("function") or {}).get("label") or "", (o.get("department") or {}).get("label") or ""]
                j = make_job(src, "%s:%s:%s" % (ats, slug, o.get("id")), o.get("name"), cname, loc,
                             "https://jobs.smartrecruiters.com/%s/%s" % (slug, o.get("id")), o.get("releasedDate"),
                             "", tags, mode="remote" if l.get("remote") else "local",
                             employment=emp_any((o.get("typeOfEmployment") or {}).get("label")))
                if (o.get("language") or {}).get("code") == "nl":
                    j["lang"] = "nl"
                jobs.append(j)
        else:
            return [], "unsupported system"
    except Exception as e:
        return [], "could not read (%s)" % e
    return jobs, None


def host_of(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def base_label(host):
    """'jobs.sendcloud.com' -> 'sendcloud'"""
    parts = [p for p in host.lower().split(".") if p]
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net") and len(parts[-1]) == 2:
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0] if parts else ""


def detect_ats(url):
    """Finds which career-page system a company uses. Returns (ats, slug, jobs) or None.
    Looks at the URL, then the page and its careers/jobs subdomains, then guesses the
    company's name as the slug on each system."""
    url = url.strip()
    if url and not re.match(r"https?://", url, re.I):
        url = "https://" + url
    cands = []

    def add(ats, slug, guessed=False):
        slug = slug if ats == "smartrecruiters" else slug.lower()
        if slug and slug.lower() not in SKIP_SLUGS and all(c[0] != ats or c[1] != slug for c in cands):
            cands.append((ats, slug, guessed))

    def scan(text, page_host=""):
        for ats, rx in ATS_PATTERNS:
            for m in rx.finditer(text or ""):
                add(ats, m.group(1))
        if page_host and "teamtailor" in (text or "").lower():
            m = re.search(r'href="https?://([^/"]+)/jobs\.rss"', text or "")
            add("teamtailor", m.group(1) if m else page_host)

    scan(url)
    host = host_of(url)
    if not cands and host:
        pages = [url]
        root = ".".join(host.split(".")[-2:]) if host.count(".") >= 2 else host
        for sub in ("careers", "jobs", "werkenbij"):
            cand = "https://%s.%s/" % (sub, root)
            if host_of(cand) != host:
                pages.append(cand)
        for p in pages:
            status, page = http_get(p, "text/html", timeout=10)
            if status == 200:
                scan(page, host_of(p))
            if cands:
                break
    if not cands:
        label = base_label(host)
        if label:
            for ats in ("recruitee", "greenhouse", "lever", "workable", "ashby", "personio"):
                add(ats, label, True)
            add("teamtailor", label + ".teamtailor.com", True)
            add("smartrecruiters", label.capitalize(), True)
            add("smartrecruiters", label, True)
    for ats, slug, guessed in cands[:14]:
        jobs, err = ats_fetch(ats, slug, ttl=0)
        if err is None and (jobs or not guessed):
            return ats, slug, jobs
    return None


def watch_add(url, name=""):
    found = detect_ats(url)
    if not found:
        return {"ok": False, "message": "No supported careers system found for that page. JobRadar can read career pages built on %s. Tip: paste the page that lists the vacancies, not the company home page." % ATS_NAMES}
    ats, slug, jobs = found
    cid = "%s:%s" % (ats, slug)
    companies = load_watchlist()
    if any(c.get("id") == cid for c in companies):
        return {"ok": False, "message": "That company is already on the list."}
    cname = clean(name) or (jobs[0]["company"] if jobs else slug.replace("-", " ").title())
    entry = {"id": cid, "ats": ats, "slug": slug, "name": cname, "url": url.strip(), "added": now_iso()}
    companies.append(entry)
    save_watchlist(companies)
    WATCH_COUNTS[cid] = {"count": len(jobs), "err": "", "at": now_iso()}
    return {"ok": True, "company": entry, "count": len(jobs), "system": ATS_LABEL.get(ats, ats)}


def watch_remove(cid):
    companies = [c for c in load_watchlist() if c.get("id") != cid]
    save_watchlist(companies)
    WATCH_COUNTS.pop(cid, None)
    return {"ok": True}


def watchlist_view():
    out = []
    for c in load_watchlist():
        e = dict(c)
        e["system"] = ATS_LABEL.get(c.get("ats"), c.get("ats"))
        e.update(WATCH_COUNTS.get(c.get("id"), {"count": None, "err": "", "at": ""}))
        out.append(e)
    return out


def src_watchlist(profile):
    name = "Watchlist"
    companies = load_watchlist()
    if not companies:
        src_status(name, "off", "no companies on the list yet", 0)
        return []
    src_status(name, "running", "0/%d companies" % len(companies), 0)
    jobs = []
    errors = []
    done = [0]

    def one(c):
        js, err = ats_fetch(c.get("ats"), c.get("slug"), c.get("name", ""))
        return c, js, err

    with ThreadPoolExecutor(max_workers=6) as ex:
        for c, js, err in ex.map(one, companies):
            if stopped():
                break
            done[0] += 1
            WATCH_COUNTS[c.get("id")] = {"count": len(js), "err": err or "", "at": now_iso()}
            if err:
                errors.append("%s: %s" % (c.get("name"), err))
            for j in js:
                # Watched companies can have offices abroad: mark jobs outside NL as limited to that place.
                if j["mode"] != "remote":
                    ok = nl_location(j["location"])
                    j["region_ok"] = True if ok is None else ok
                    if ok is False:
                        j["region"] = j["location"]
            jobs.extend(js)
            src_status(name, "running", "%d/%d companies" % (done[0], len(companies)), len(jobs))
    msg = "%d companies" % len(companies)
    if errors:
        msg += "; problems: " + "; ".join(errors[:3])
    src_status(name, "done", msg, len(jobs))
    return jobs


# ----------------------------------------------------------------------
# Source: staffing agencies. Randstad's public vacancy search page is
# server-rendered and its robots.txt allows crawling of /vacatures, so
# JobRadar fetches one results page per search term, spaced out and
# cached for an hour. Other agencies have no usable page and are links.
# ----------------------------------------------------------------------

AGENCY_SITES = [
    # Randstad renders its results in the page; Tempo-Team (same platform) loads them
    # through a JSON call that the page itself makes.
    {"name": "Randstad", "kind": "html", "base": "https://www.randstad.nl/vacatures", "host": "https://www.randstad.nl"},
    {"name": "Tempo-Team", "kind": "json", "base": "https://www.tempo-team.nl/vacatures/", "host": "https://www.tempo-team.nl", "ref": "r279_r1_r1"},
]
AG_CARD_RE = re.compile(r'<article class="card item" id="vacancy_(\d+)">(.*?)</article>', re.S)
NL_MONTHS = {"januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6, "juli": 7,
             "augustus": 8, "september": 9, "oktober": 10, "november": 11, "december": 12}


def nl_date(text):
    """'09 september 2026' -> '2026-09-09'"""
    m = re.match(r"\s*(\d{1,2})\s+([a-z]+)\s+(\d{4})", str(text or "").lower())
    if m and m.group(2) in NL_MONTHS:
        return "%s-%02d-%02d" % (m.group(3), NL_MONTHS[m.group(2)], int(m.group(1)))
    return ""


def ag_parse_json(site, text):
    jobs = []
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict) or "vacancies" not in data:
        return None
    for v in data.get("vacancies") or []:
        meta = v.get("meta") or {}
        title = v.get("title") or ""
        if not title:
            continue
        href = v.get("detailUrl") or ""
        url = (site["host"] + href) if href.startswith("/") else (href or site["base"])
        emp_text = str(meta.get("employmentTypeSchemaOrg") or "")
        if "PART_TIME" in emp_text and "FULL_TIME" not in emp_text:
            employment = "part-time"
        elif "FULL_TIME" in emp_text:
            employment = "full-time"
        elif "INTERN" in emp_text:
            employment = "internship"
        else:
            employment = ""
        salary = ""
        if meta.get("salary"):
            salary = "EUR %s per %s" % (meta["salary"], meta.get("salaryTimeUnit") or "uur")
        company = v.get("company") or site["name"]
        tags = [c.get("description", "") for c in (v.get("contractType") or []) if isinstance(c, dict)]
        if meta.get("hours"):
            tags.append(str(meta["hours"]))
        jobs.append(make_job(site["name"], v.get("vacancyNumber") or href, title, company, meta.get("location") or "",
                             url, nl_date(v.get("date")), v.get("summary") or "", tags,
                             mode="remote" if v.get("workFromHome") else "local", employment=employment))
    return jobs


def ag_text(pattern, block, limit=600):
    m = re.search(pattern, block, re.S)
    return strip_html(m.group(1), limit) if m else ""


def ag_parse(site, text):
    jobs = []
    for m in AG_CARD_RE.finditer(text):
        vid, block = m.group(1), m.group(2)
        lm = re.search(r'<a href="([^"]+)"', block)
        href = htmllib.unescape(lm.group(1)) if lm else ""
        url = (site["host"] + href) if href.startswith("/") else (href or site["base"])
        heading = ag_text(r'<div class="card__title">\s*<h2>(.*?)</h2>', block, 200)
        if not heading:
            continue
        title, company = heading, site["name"]
        if ", " in heading:
            title, company = heading.rsplit(", ", 1)
        place = ag_text(r'vacancy-tile__detail--place.*?<span class="simplelist__item--text">(.*?)</span>', block, 80)
        hours = ag_text(r'<span class="simplelist__item--text">(\d+)\s*uur</span>', block, 10)
        desc = ag_text(r'<div class="card__description">(.*?)</div>', block)
        label = ag_text(r'<div class="yacht-label__container">(.*?)</div>', block, 60)
        employment = ""
        if hours.isdigit():
            employment = "part-time" if int(hours) < 32 else "full-time"
        jobs.append(make_job(site["name"], vid, title, company, place, url, "", desc,
                             [label] if label else [], mode="local", employment=employment))
    return jobs


def src_agencies(profile):
    name = "Agencies"
    if not profile.get("want_local", True):
        src_status(name, "off", "local search switched off", 0)
        return []
    terms = search_terms(profile)
    total = len(terms) * len(AGENCY_SITES)
    jobs = {}
    done = [0]
    problems = []
    src_status(name, "running", "0/%d searches" % total, 0)

    def run_site(site):
        found_all = []
        for term in terms:
            if stopped():
                break
            params = {"zoekterm": term, "locatie": profile.get("city", "Eindhoven").lower(),
                      "afstand": int(profile.get("radius_km", 40))}
            if site["kind"] == "json":
                params.update({"_hn:type": "resource", "_hn:ref": site["ref"], "pagina": 1})
            url = site["base"] + "?" + urllib.parse.urlencode(params)
            status, text, cached = cached_get(url, 3600, "application/json, text/html")
            done[0] += 1
            if status != 200:
                problems.append("%s answered HTTP %s" % (site["name"], status))
                src_status(name, "running", "%s '%s' failed (HTTP %s)" % (site["name"], term, status), len(jobs))
                continue
            found = ag_parse_json(site, text) if site["kind"] == "json" else ag_parse(site, text)
            if found is None:
                problems.append("%s changed its site, results unreadable" % site["name"])
                break
            found_all.extend(found)
            src_status(name, "running", "%d/%d searches" % (done[0], total), len(jobs) + len(found_all))
            if not cached:
                time.sleep(1.2)
        return found_all

    with ThreadPoolExecutor(max_workers=max(1, len(AGENCY_SITES))) as ex:
        for found in ex.map(run_site, AGENCY_SITES):
            for j in found:
                jobs[j["id"]] = j
    msg = "%d searches on %s" % (total, ", ".join(s["name"] for s in AGENCY_SITES))
    if problems:
        msg += "; " + "; ".join(sorted(set(problems))[:2])
    src_status(name, "done", msg, len(jobs))
    return list(jobs.values())


GENERIC_WORDS = {"online", "media", "social", "performance", "digital", "creation", "manager", "specialist",
                 "medewerker", "junior", "senior", "editor"}

NL_PLACES = ["netherlands", "nederland", "the hague", "den haag", "amsterdam", "rotterdam", "utrecht", "eindhoven",
             "tilburg", "breda", "den bosch", "s-hertogenbosch", "hertogenbosch", "groningen", "nijmegen", "arnhem",
             "haarlem", "hilversum", "amersfoort", "almere", "maastricht", "enschede", "zwolle", "leiden", "delft",
             "veldhoven", "helmond", "venlo", "apeldoorn", "zaandam", "dordrecht", "leeuwarden", "deventer", "best",
             "son en breugel", "waalre", "oss", "roermond", "weert", "noord-brabant", "brabant", "limburg", "gelderland",
             "holland", "nl"]
ANYWHERE = ["remote", "anywhere", "worldwide", "global", "europe", "emea", "international", "work from home", "thuiswerk"]


def nl_location(loc):
    """True for a Dutch or remote location, False for a named place elsewhere, None if unknown."""
    t = " " + norm(loc) + " "
    if not t.strip():
        return None
    if any(" " + p + " " in t or t.strip() == p for p in NL_PLACES):
        return True
    if any(w in t for w in ANYWHERE):
        return True
    return False


def title_word_hit(job, profile):
    """True when a meaningful word of the skills or search terms, or a Dutch equivalent, is in the title."""
    # Ignore the "bij <company> in <city>" tail that agencies put in titles.
    core = re.split(r"\b(?:bij|at|in|voor|for|te)\b", norm(job.get("title", "")))[0]
    title_words = set(core.split())
    for t in list(profile.get("keywords", [])) + list(profile.get("skills", [])):
        for w in norm(t).split():
            if len(w) <= 3 or w in GENERIC_WORDS:
                continue
            if w in title_words or any(s in title_words for s in WORD_SYNONYMS.get(w, [])):
                return True
    return False


SOURCES = {
    "Jooble": src_jooble,
    "Agencies": src_agencies,
    "Watchlist": src_watchlist,
    "Remotive": src_remotive,
    "We Work Remotely": src_wwr,
    "Jobicy": src_jobicy,
    "RemoteOK": src_remoteok,
}


# ----------------------------------------------------------------------
# Search orchestration
# ----------------------------------------------------------------------

def run_search(profile):
    results = {}

    def wrap(name):
        try:
            return name, SOURCES[name](profile)
        except Exception as e:
            src_status(name, "error", "failed: %s" % e, 0)
            return name, []

    with ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        for name, jobs in ex.map(wrap, SOURCE_ORDER):
            results[name] = jobs

    # Score everything, add the distance from her city, keep relevant jobs, drop duplicates.
    home_places = city_coords(profile.get("city", ""))
    home = home_places[0] if home_places else None
    for name in SOURCE_ORDER:
        for job in results.get(name, []):
            score_job(job, profile)
            job["km"] = job_distance(job, home)
    watch_jobs = sorted(results.get("Watchlist", []), key=lambda j: (-j["score"], j["age_days"]))[:600]
    merged = {}
    dupkeys = {}
    for name in SOURCE_ORDER:
        for job in results.get(name, []):
            if name == "Agencies":
                if job["score"] < 20 and not title_word_hit(job, profile):
                    continue
            elif name not in KEYWORD_SOURCES and job["score"] < 20:
                continue
            if not profile.get("want_remote", True) and job["mode"] == "remote":
                continue
            key = norm(job["title"])[:60] + "|" + norm(job["company"])[:30]
            if key in dupkeys:
                continue
            dupkeys[key] = job["id"]
            merged[job["id"]] = job

    with LOCK:
        stamp = now_iso()
        for jid, job in merged.items():
            job["is_new"] = jid not in STATE["seen"]
            STATE["seen"].setdefault(jid, stamp)
        jobs = sorted(merged.values(), key=lambda j: (-j["score"], j["age_days"]))
        STATE["last"] = {"at": stamp, "jobs": jobs, "watch_jobs": watch_jobs, "sources": json.loads(json.dumps(SEARCH["sources"]))}
        SEARCH["jobs"] = jobs
        SEARCH["running"] = False
        SEARCH["message"] = "Found %d matching job%s" % (len(jobs), "" if len(jobs) == 1 else "s")
    persist()


def start_search():
    with LOCK:
        if SEARCH["running"]:
            return False
        SEARCH.update({"running": True, "stop": False, "started": now_iso(), "jobs": [], "message": "Searching..."})
        SEARCH["sources"] = {n: {"status": "pending", "msg": "", "count": 0} for n in SOURCE_ORDER}
        profile = json.loads(json.dumps(STATE["profile"]))
    threading.Thread(target=run_search, args=(profile,), daemon=True).start()
    return True


# ----------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------

def parse_list(value):
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[,\n;]+", str(value or ""))
    out = []
    for x in items:
        x = clean(x)
        if x and x.lower() not in [o.lower() for o in out]:
            out.append(x)
    return out[:12]


def clean_profile(p):
    prof = dict(STATE["profile"])
    prof["skills"] = parse_list(p.get("skills", prof["skills"]))
    prof["keywords"] = parse_list(p.get("keywords", prof["keywords"]))
    prof["exclude"] = parse_list(p.get("exclude", prof["exclude"]))
    prof["city"] = clean(p.get("city", prof["city"]))[:60] or "Eindhoven"
    prof["country"] = clean(p.get("country", prof["country"]))[:60] or "Netherlands"
    try:
        prof["radius_km"] = max(5, min(200, int(p.get("radius_km", prof["radius_km"]))))
    except Exception:
        pass
    try:
        prof["days"] = max(1, min(90, int(p.get("days", prof["days"]))))
    except Exception:
        pass
    prof["want_local"] = bool(p.get("want_local", prof["want_local"]))
    prof["want_remote"] = bool(p.get("want_remote", prof["want_remote"]))
    return prof


class Handler(BaseHTTPRequestHandler):
    server_version = "JobRadar/1.0"

    def log_message(self, fmt, *args):
        if "/api/search/status" in (args[0] if args else ""):
            return
        sys.stdout.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def state_snapshot(self):
        with LOCK:
            return {
                "profile": STATE["profile"],
                "saved": STATE["saved"],
                "applied": STATE["applied"],
                "hidden": STATE["hidden"],
                "last": {k: v for k, v in STATE["last"].items() if k != "watch_jobs"},
                "watchlist": watchlist_view(),
                "watch_jobs": STATE["last"].get("watch_jobs", []),
                "search": {k: SEARCH[k] for k in ("running", "started", "sources", "message")},
            }

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(INDEX_FILE, "rb") as f:
                    body = f.read()
            except Exception:
                self.send_error(500, "index.html is missing next to server.py")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            self.send_json(self.state_snapshot())
        elif path == "/api/search/status":
            with LOCK:
                snap = {k: SEARCH[k] for k in ("running", "started", "sources", "message")}
                snap["jobs"] = SEARCH["jobs"] if not SEARCH["running"] else []
                snap["at"] = STATE["last"].get("at", "")
            self.send_json(snap)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self.read_json()
        if path == "/api/profile":
            with LOCK:
                STATE["profile"] = clean_profile(body)
            persist()
            self.send_json({"ok": True, "profile": STATE["profile"]})
        elif path == "/api/search":
            if body:
                with LOCK:
                    STATE["profile"] = clean_profile(body)
                persist()
            ok = start_search()
            self.send_json({"ok": ok, "message": "started" if ok else "already running"}, 200 if ok else 409)
        elif path == "/api/search/stop":
            with LOCK:
                SEARCH["stop"] = True
            self.send_json({"ok": True})
        elif path == "/api/job/save":
            jid = body.get("id", "")
            with LOCK:
                if jid in STATE["saved"]:
                    del STATE["saved"][jid]
                    STATE["applied"].pop(jid, None)
                elif isinstance(body.get("job"), dict):
                    STATE["saved"][jid] = body["job"]
            persist()
            self.send_json({"ok": True, "saved": list(STATE["saved"].keys())})
        elif path == "/api/job/applied":
            jid = body.get("id", "")
            with LOCK:
                if jid in STATE["applied"]:
                    del STATE["applied"][jid]
                else:
                    STATE["applied"][jid] = now_iso()
                    if jid not in STATE["saved"] and isinstance(body.get("job"), dict):
                        STATE["saved"][jid] = body["job"]
            persist()
            self.send_json({"ok": True})
        elif path == "/api/job/hide":
            jid = body.get("id", "")
            with LOCK:
                STATE["hidden"][jid] = now_iso()
                STATE["saved"].pop(jid, None)
                STATE["applied"].pop(jid, None)
            persist()
            self.send_json({"ok": True})
        elif path == "/api/job/unhide":
            with LOCK:
                STATE["hidden"].pop(body.get("id", ""), None)
            persist()
            self.send_json({"ok": True})
        elif path == "/api/job/unhide_all":
            with LOCK:
                STATE["hidden"] = {}
            persist()
            self.send_json({"ok": True})
        elif path == "/api/watch/add":
            self.send_json(watch_add(str(body.get("url", "")), str(body.get("name", ""))))
        elif path == "/api/watch/remove":
            self.send_json(watch_remove(str(body.get("id", ""))))
        else:
            self.send_error(404)


class ExclusiveServer(ThreadingHTTPServer):
    """Refuses to share its port. Python's HTTPServer sets SO_REUSEADDR, which on Windows
    lets a second JobRadar bind the same port and steal half the requests."""
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        ThreadingHTTPServer.server_bind(self)


def find_running(ports):
    """URL of a JobRadar that is already running on one of these ports, or None."""
    for p in ports:
        url = "http://127.0.0.1:%d/api/state" % p
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=1) as r:
                if "JobRadar" in (r.headers.get("Server") or ""):
                    return "http://127.0.0.1:%d/" % p
        except Exception:
            continue
    return None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    load_state()
    forced = os.environ.get("JOBRADAR_PORT")
    ports = [int(forced)] if forced else list(PORTS)
    if not forced:
        running = find_running(ports)
        if running:
            print("JobRadar is already running at %s" % running)
            print("Opening it in your browser. You can close this window.")
            if not os.environ.get("JOBRADAR_NO_BROWSER"):
                webbrowser.open(running)
            time.sleep(2)
            return
    server = None
    port = None
    for p in ports:
        try:
            server = ExclusiveServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if server is None:
        print("Could not open a local port. Close other JobRadar windows and try again.")
        sys.exit(1)
    url = "http://127.0.0.1:%d/" % port
    print("JobRadar is running at %s" % url)
    print("Keep this window open while you use it. Close it to stop JobRadar.")
    if not os.environ.get("JOBRADAR_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
