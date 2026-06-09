"""
JStoutCollectibles — Full dashboard: card lookup, MLB data, marketplace, newsletter.
Port 5051
"""
import os, json, re, base64, time
from pathlib import Path
from datetime import date, datetime, timedelta
from flask import Flask, render_template, request, jsonify
from anthropic import Anthropic
from dotenv import load_dotenv
import requests as req

load_dotenv()
app = Flask(__name__)
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

LISTINGS_FILE = Path(__file__).parent / "listings.json"
SUBS_FILE     = Path(__file__).parent / "subscribers.json"
CACHE         = {}  # simple in-memory cache {key: (ts, data)}
CACHE_TTL     = 300  # 5 minutes

MLB_BASE = "https://statsapi.mlb.com/api/v1"
REDS_ID  = 113


def cached(key, fn, ttl=CACHE_TTL):
    now = time.time()
    if key in CACHE and now - CACHE[key][0] < ttl:
        return CACHE[key][1]
    data = fn()
    CACHE[key] = (now, data)
    return data


# ── helpers ──────────────────────────────────────────────────────────────────
def load_listings():
    return json.loads(LISTINGS_FILE.read_text(encoding="utf-8")).get("listings", []) if LISTINGS_FILE.exists() else []

def save_listings(data):
    LISTINGS_FILE.write_text(json.dumps({"listings": data}, indent=2), encoding="utf-8")

def load_subs():
    return json.loads(SUBS_FILE.read_text(encoding="utf-8")).get("subscribers", []) if SUBS_FILE.exists() else []

def save_subs(data):
    SUBS_FILE.write_text(json.dumps({"subscribers": data}, indent=2), encoding="utf-8")


# ── MLB API routes ────────────────────────────────────────────────────────────

@app.route("/api/mlb/scores")
def mlb_scores():
    def fetch():
        today = date.today().isoformat()
        r = req.get(f"{MLB_BASE}/schedule?sportId=1&date={today}&hydrate=linescore", timeout=12)
        games_raw = r.json().get("dates", [{}])[0].get("games", []) if r.json().get("dates") else []
        games = []
        for g in games_raw:
            status = g.get("status", {}).get("detailedState", "")
            ls = g.get("linescore", {})
            away_team = g["teams"]["away"]["team"]["name"]
            home_team = g["teams"]["home"]["team"]["name"]
            away_score = g["teams"]["away"].get("score", "")
            home_score = g["teams"]["home"].get("score", "")
            inning = ls.get("currentInningOrdinal", "")
            games.append({
                "away": away_team, "home": home_team,
                "away_score": away_score, "home_score": home_score,
                "status": status, "inning": inning,
                "is_reds": REDS_ID in [
                    g["teams"]["away"]["team"]["id"],
                    g["teams"]["home"]["team"]["id"]
                ],
            })
        return games
    return jsonify({"scores": cached("scores", fetch)})


@app.route("/api/mlb/reds-schedule")
def reds_schedule():
    def fetch():
        start = date.today().isoformat()
        end   = (date.today() + timedelta(days=21)).isoformat()
        r = req.get(
            f"{MLB_BASE}/schedule?sportId=1&teamId={REDS_ID}&startDate={start}&endDate={end}&hydrate=linescore",
            timeout=12
        )
        games = []
        for d in r.json().get("dates", []):
            for g in d.get("games", []):
                status = g["status"]["detailedState"]
                is_home = g["teams"]["home"]["team"]["id"] == REDS_ID
                opp = g["teams"]["away"]["team"]["name"] if is_home else g["teams"]["home"]["team"]["name"]
                opp_abbr = g["teams"]["away"]["team"].get("abbreviation", opp[:3].upper()) if is_home else g["teams"]["home"]["team"].get("abbreviation", opp[:3].upper())
                game_dt = g.get("gameDate", "")
                try:
                    dt = datetime.fromisoformat(game_dt.replace("Z", "+00:00"))
                    game_time = dt.strftime("%-I:%M %p ET")
                    game_date = dt.strftime("%b %-d")
                except Exception:
                    game_time = ""
                    game_date = game_dt[:10]
                reds_score = g["teams"]["home"].get("score") if is_home else g["teams"]["away"].get("score")
                opp_score  = g["teams"]["away"].get("score") if is_home else g["teams"]["home"].get("score")
                games.append({
                    "date": game_date, "time": game_time,
                    "opponent": opp, "opp_abbr": opp_abbr,
                    "is_home": is_home,
                    "status": status,
                    "reds_score": reds_score,
                    "opp_score": opp_score,
                })
        return games[:8]
    return jsonify({"games": cached("reds_sched", fetch, ttl=600)})


@app.route("/api/mlb/rookies")
def mlb_rookies():
    def fetch():
        r = req.get(
            f"{MLB_BASE}/stats?stats=season&group=hitting&gameType=R&season=2026"
            f"&sportId=1&limit=300&sortStat=homeRuns&order=desc",
            timeout=15
        )
        splits = r.json().get("stats", [{}])[0].get("splits", [])
        # Get rookie-status players from the people endpoint
        # For simplicity, flag well-known 2026 rookies or those with < 400 PA last year
        rookie_names = {
            "Jackson Holliday", "Jackson Chourio", "Wyatt Langford",
            "Paul Skenes", "Colt Keith", "Jordan Walker",
            "Evan Carter", "Masyn Winn", "Andy Pages",
            "James Wood", "Junior Caminero", "Kyle Manzardo",
            "Xavier Isaac", "Heston Kjerstad", "Spencer Jones",
            "Adley Rutschman", "Gunnar Henderson", "Anthony Volpe",
        }
        rookies = []
        for s in splits:
            name = s.get("player", {}).get("fullName", "")
            stat = s.get("stat", {})
            team = s.get("team", {}).get("name", "")
            pa   = int(stat.get("plateAppearances") or stat.get("atBats") or 0)
            if name in rookie_names and pa >= 30:
                rookies.append({
                    "name": name, "team": team,
                    "avg": stat.get("avg", ".000"),
                    "hr": stat.get("homeRuns", 0),
                    "rbi": stat.get("rbi", 0),
                    "sb": stat.get("stolenBases", 0),
                    "ops": stat.get("ops", ".000"),
                    "pa": pa,
                })
        # Also take top by ops from full list if fewer than 6 rookies found
        if len(rookies) < 6:
            all_by_ops = sorted(splits, key=lambda x: float(x.get("stat",{}).get("ops","0") or 0), reverse=True)
            for s in all_by_ops:
                name = s.get("player",{}).get("fullName","")
                if any(r["name"] == name for r in rookies):
                    continue
                stat = s.get("stat",{})
                pa = int(stat.get("plateAppearances") or stat.get("atBats") or 0)
                if pa < 30:
                    continue
                # crude rookie heuristic: limit to known 2026 debuts
                if name in rookie_names:
                    team = s.get("team",{}).get("name","")
                    rookies.append({
                        "name": name, "team": team,
                        "avg": stat.get("avg", ".000"),
                        "hr": stat.get("homeRuns", 0),
                        "rbi": stat.get("rbi", 0),
                        "sb": stat.get("stolenBases", 0),
                        "ops": stat.get("ops", ".000"),
                        "pa": pa,
                    })
                if len(rookies) >= 8:
                    break
        # Fallback: top hitters overall if no rookies found
        if not rookies:
            for s in splits[:8]:
                stat = s.get("stat",{})
                rookies.append({
                    "name": s.get("player",{}).get("fullName",""),
                    "team": s.get("team",{}).get("name",""),
                    "avg": stat.get("avg",".000"),
                    "hr": stat.get("homeRuns",0),
                    "rbi": stat.get("rbi",0),
                    "sb": stat.get("stolenBases",0),
                    "ops": stat.get("ops",".000"),
                    "pa": int(stat.get("plateAppearances") or 0),
                })
        return rookies[:8]
    return jsonify({"rookies": cached("rookies", fetch, ttl=3600)})


@app.route("/api/mlb/standings")
def mlb_standings():
    def fetch():
        r = req.get(
            f"{MLB_BASE}/standings?leagueId=103,104&season=2026&standingsTypes=regularSeason",
            timeout=10
        )
        reds_rec = {}
        for record in r.json().get("records", []):
            for t in record.get("teamRecords", []):
                if t["team"]["id"] == REDS_ID:
                    reds_rec = {
                        "wins": t.get("wins", 0),
                        "losses": t.get("losses", 0),
                        "gb": t.get("gamesBack", "—"),
                        "pct": t.get("winningPercentage", ".000"),
                        "div": record.get("division", {}).get("name", "NL Central"),
                        "streak": t.get("streak", {}).get("streakCode", ""),
                        "last10": t.get("records", {}).get("splitRecords", []),
                    }
        return reds_rec
    return jsonify({"standings": cached("standings", fetch, ttl=3600)})


@app.route("/api/release-calendar")
def release_calendar():
    releases = [
        {"date": "Jun 11, 2026", "title": "2026 Topps Series 2 Baseball",        "brand": "Topps",   "sport": "MLB", "hot": True},
        {"date": "Jun 18, 2026", "title": "2026 Bowman Draft Picks & Prospects",  "brand": "Bowman",  "sport": "MLB", "hot": True},
        {"date": "Jun 25, 2026", "title": "2026 Panini Donruss Baseball",         "brand": "Panini",  "sport": "MLB", "hot": False},
        {"date": "Jul 2,  2026", "title": "2026 Topps Chrome Update",             "brand": "Topps",   "sport": "MLB", "hot": True},
        {"date": "Jul 9,  2026", "title": "2026 Prizm Draft Picks Basketball",    "brand": "Panini",  "sport": "NBA", "hot": True},
        {"date": "Jul 16, 2026", "title": "2026 Topps Stadium Club",              "brand": "Topps",   "sport": "MLB", "hot": False},
        {"date": "Jul 23, 2026", "title": "2026 Bowman Platinum",                 "brand": "Bowman",  "sport": "MLB", "hot": False},
        {"date": "Aug 5, 2026",  "title": "2026 Panini Prizm Baseball",           "brand": "Panini",  "sport": "MLB", "hot": True},
    ]
    return jsonify({"releases": releases})


@app.route("/api/card-values")
def card_values():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"error": "query required"}), 400

    q_enc = req.utils.quote(query)
    sources = [
        {
            "name": "eBay Sold",
            "icon": "🛒",
            "url": f"https://www.ebay.com/sch/i.html?_nkw={q_enc}&LH_Sold=1&LH_Complete=1&_sop=13",
            "desc": "Recently sold listings — most reliable pricing",
        },
        {
            "name": "eBay Active",
            "icon": "📦",
            "url": f"https://www.ebay.com/sch/i.html?_nkw={q_enc}&_sop=15",
            "desc": "Current buy-it-now listings",
        },
        {
            "name": "Beckett",
            "icon": "📊",
            "url": f"https://www.beckett.com/search/?q={q_enc}",
            "desc": "Official graded card values",
        },
        {
            "name": "Fanatics",
            "icon": "🏆",
            "url": f"https://www.fanatics.com/search#query={q_enc}&categoryId=23&isCollectibles=true",
            "desc": "New & graded cards from Fanatics",
        },
        {
            "name": "Facebook Marketplace",
            "icon": "📘",
            "url": f"https://www.facebook.com/marketplace/search/?query={q_enc}%20card",
            "desc": "Local & national FB sellers",
        },
        {
            "name": "130 Point",
            "icon": "💎",
            "url": f"https://www.130point.com/sales/search?q={q_enc}",
            "desc": "eBay sold price tracking & analytics",
        },
    ]
    return jsonify({"sources": sources, "query": query})


# ── Card lookup / marketplace / newsletter (existing) ─────────────────────────

@app.route("/")
def index():
    return render_template("index.html", listings=load_listings())

@app.route("/api/identify-card", methods=["POST"])
def identify_card():
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400
    file = request.files["image"]
    img_bytes = file.read()
    b64 = base64.standard_b64encode(img_bytes).decode()
    media_type = file.content_type or "image/jpeg"
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=800,
            messages=[{"role":"user","content":[
                {"type":"image","source":{"type":"base64","media_type":media_type,"data":b64}},
                {"type":"text","text":"""Identify this sports/trading card. Return ONLY valid JSON:
{"player":"Full name","year":"Year","brand":"Brand","set":"Set name","card_number":"Number if visible",
"sport":"Sport","team":"Team","rookie_card":false,"parallel":null,
"condition":"Estimated condition","estimated_value":"Est. value range USD",
"notable":"Key collectibility fact","confidence":"high/medium/low"}"""}
            ]}],
        )
        raw = resp.content[0].text.strip()
        if "```" in raw:
            raw = re.sub(r"```(?:json)?","",raw).strip().strip("```").strip()
        return jsonify({"card": json.loads(raw)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/lookup-card", methods=["POST"])
def lookup_card():
    data = request.get_json()
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query required"}), 400
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=700,
            messages=[{"role":"user","content":
                f'Sports card lookup: "{query}"\nReturn ONLY valid JSON:\n'
                '{"player":"Name","year":"Year","brand":"Brand","set":"Set","card_number":"#","sport":"Sport",'
                '"team":"Team","rookie_card":false,"parallel":null,"condition":"Unknown",'
                '"estimated_value":"PSA 9 value range USD","notable":"Key fact","confidence":"high/medium/low"}'}],
        )
        raw = resp.content[0].text.strip()
        if "```" in raw:
            raw = re.sub(r"```(?:json)?","",raw).strip().strip("```").strip()
        return jsonify({"card": json.loads(raw)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/list-item", methods=["POST"])
def list_item():
    import uuid
    data = request.get_json()
    for f in ["title","price","condition","seller_email"]:
        if not data.get(f):
            return jsonify({"error": f"Missing: {f}"}), 400
    listing = {
        "id": str(uuid.uuid4())[:8],
        "title": data["title"].strip(),
        "price": float(data["price"]),
        "condition": data["condition"].strip(),
        "description": (data.get("description") or "").strip(),
        "seller_email": data["seller_email"].strip().lower(),
        "player": (data.get("player") or "").strip(),
        "year": (data.get("year") or "").strip(),
        "brand": (data.get("brand") or "").strip(),
        "card_number": (data.get("card_number") or "").strip(),
        "date_listed": date.today().isoformat(),
        "status": "active",
    }
    listings = load_listings()
    listings.insert(0, listing)
    save_listings(listings)
    return jsonify({"status": "listed", "listing": listing})

@app.route("/api/listings")
def get_listings():
    return jsonify({"listings": load_listings()})

@app.route("/api/subscribe", methods=["POST"])
def subscribe():
    data = request.get_json()
    email = (data.get("email") or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"error": "Invalid email"}), 400
    subs = load_subs()
    if email in subs:
        return jsonify({"status": "already_subscribed"})
    subs.append(email)
    save_subs(subs)
    return jsonify({"status": "subscribed"})

@app.route("/api/jordan-prices")
def jordan_prices():
    from tools.fetch_jordan import fetch_jordan_grades
    eights, premium = fetch_jordan_grades()
    return jsonify({"grades_8plus": eights, "premium": premium})


@app.route("/api/megabox-rankings")
def megabox_rankings():
    from tools.fetch_topps_megabox import fetch_megabox_rankings
    return jsonify({"boxes": fetch_megabox_rankings()})


if __name__ == "__main__":
    app.run(debug=True, port=5051)
