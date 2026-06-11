import streamlit as st
import requests
import json
from collections import defaultdict
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
LEAGUE_ID = "1312110853201354752"
BASE = "https://api.sleeper.app/v1"

# ── Sleeper API helpers ───────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def get_league():
    return requests.get(f"{BASE}/league/{LEAGUE_ID}").json()

@st.cache_data(ttl=300)
def get_rosters():
    return requests.get(f"{BASE}/league/{LEAGUE_ID}/rosters").json()

@st.cache_data(ttl=300)
def get_users():
    return requests.get(f"{BASE}/league/{LEAGUE_ID}/users").json()

@st.cache_data(ttl=300)
def get_transactions(week: int):
    return requests.get(f"{BASE}/league/{LEAGUE_ID}/transactions/{week}").json()

@st.cache_data(ttl=3600)
def get_all_players():
    """Full Sleeper player DB — heavy call, cached for 1 hour."""
    return requests.get(f"{BASE}/players/nfl").json()

@st.cache_data(ttl=300)
def get_matchups(week: int):
    return requests.get(f"{BASE}/league/{LEAGUE_ID}/matchups/{week}").json()

# ── Data processing ───────────────────────────────────────────────────────────

def build_user_map(users, rosters):
    """roster_id → {display_name, avatar, user_id}"""
    uid_to_user = {u["user_id"]: u for u in users}
    mapping = {}
    for r in rosters:
        uid = r.get("owner_id")
        user = uid_to_user.get(uid, {})
        mapping[r["roster_id"]] = {
            "display_name": user.get("display_name", f"Manager {r['roster_id']}"),
            "avatar": user.get("avatar"),
            "user_id": uid,
        }
    return mapping

def get_season_weeks(league):
    """Return (current_week, playoff_start, season_type)"""
    settings = league.get("settings", {})
    return (
        league.get("settings", {}).get("leg", 1),
        settings.get("playoff_week_start", 15),
    )

def collect_trades(total_weeks):
    """Pull every completed trade across all weeks."""
    trades = []
    for week in range(1, total_weeks + 1):
        txns = get_transactions(week)
        if not txns:
            continue
        for t in txns:
            if t.get("type") == "trade" and t.get("status") == "complete":
                t["_week"] = week
                trades.append(t)
    return trades

def collect_player_points(total_weeks):
    """
    Returns dict: { player_id: { week: pts } }
    Built from matchup data across all scored weeks.
    """
    player_week_pts = defaultdict(dict)
    for week in range(1, total_weeks + 1):
        matchups = get_matchups(week)
        if not matchups:
            continue
        for m in matchups:
            pts_map = m.get("players_points", {})
            for pid, pts in pts_map.items():
                player_week_pts[pid][week] = pts
    return player_week_pts

def analyze_trades(trades, player_week_pts, user_map, total_weeks):
    """
    For each trade, compare the fantasy points each side accumulated
    *after* the trade week through the end of the season.
    Returns a list of trade result dicts.
    """
    results = []

    for trade in trades:
        trade_week = trade["_week"]
        post_weeks = range(trade_week + 1, total_weeks + 1)

        adds = trade.get("adds") or {}        # {player_id: roster_id}
        drops = trade.get("drops") or {}      # {player_id: roster_id}  (who gave them up)

        # Build {roster_id: [player_ids received]}
        side_received = defaultdict(list)
        for pid, roster_id in adds.items():
            side_received[roster_id].append(pid)

        if len(side_received) < 2:
            continue  # skip malformed

        # Score each side
        side_scores = {}
        for roster_id, players in side_received.items():
            total = 0.0
            for pid in players:
                for w in post_weeks:
                    total += player_week_pts.get(str(pid), {}).get(w, 0.0)
                    total += player_week_pts.get(pid, {}).get(w, 0.0)
            side_scores[roster_id] = {"players": players, "pts": round(total, 2)}

        rosters = list(side_scores.keys())
        if len(rosters) < 2:
            continue

        # Determine winner (highest pts received)
        sorted_sides = sorted(rosters, key=lambda r: side_scores[r]["pts"], reverse=True)
        winner_id = sorted_sides[0]
        loser_id  = sorted_sides[1]

        winner_pts = side_scores[winner_id]["pts"]
        loser_pts  = side_scores[loser_id]["pts"]
        margin = round(winner_pts - loser_pts, 2)

        results.append({
            "trade_week": trade_week,
            "trade_id": trade.get("transaction_id", ""),
            "winner_roster": winner_id,
            "loser_roster": loser_id,
            "winner_pts": winner_pts,
            "loser_pts": loser_pts,
            "margin": margin,
            "side_scores": side_scores,
            "winner_players": side_scores[winner_id]["players"],
            "loser_players": side_scores[loser_id]["players"],
        })

    return results

def build_leaderboard(trade_results, user_map):
    """Aggregate per-manager stats."""
    stats = defaultdict(lambda: {
        "wins": 0, "losses": 0, "pts_gained": 0.0,
        "pts_surrendered": 0.0, "net": 0.0, "trades": 0
    })

    for t in trade_results:
        wr, lr = t["winner_roster"], t["loser_roster"]
        stats[wr]["wins"] += 1
        stats[wr]["pts_gained"] += t["winner_pts"]
        stats[wr]["pts_surrendered"] += t["loser_pts"]
        stats[lr]["losses"] += 1
        stats[lr]["pts_gained"] += t["loser_pts"]
        stats[lr]["pts_surrendered"] += t["winner_pts"]
        for rid in [wr, lr]:
            stats[rid]["trades"] += 1

    for rid, s in stats.items():
        s["net"] = round(s["pts_gained"] - s["pts_surrendered"], 2)
        s["pts_gained"] = round(s["pts_gained"], 2)
        s["pts_surrendered"] = round(s["pts_surrendered"], 2)
        s["display_name"] = user_map.get(rid, {}).get("display_name", f"Roster {rid}")
        s["avatar"] = user_map.get(rid, {}).get("avatar")
        s["roster_id"] = rid

    leaderboard = sorted(stats.values(), key=lambda x: (-x["wins"], -x["net"]))
    return leaderboard

# ── Player name helper ────────────────────────────────────────────────────────

@st.cache_data(ttl=3600)
def get_player_name_map():
    players = get_all_players()
    return {
        pid: f"{p.get('first_name','')} {p.get('last_name','')}".strip() or pid
        for pid, p in players.items()
    }

# ── UI helpers ────────────────────────────────────────────────────────────────

def avatar_url(avatar_id):
    if avatar_id:
        return f"https://sleepercdn.com/avatars/thumbs/{avatar_id}"
    return "https://sleepercdn.com/images/v2/icons/player_icons/default.jpg"

def rank_badge(rank):
    if rank == 1:   return "🥇"
    if rank == 2:   return "🥈"
    if rank == 3:   return "🥉"
    return f"#{rank}"

# ── Page ──────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Trade Report Card",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;600;800&family=DM+Mono:wght@400;500&display=swap');

html, body, [class*="css"] {
    font-family: 'Syne', sans-serif;
    background: #0d0f14;
    color: #e8eaf0;
}

/* Kill default Streamlit padding */
.block-container { padding: 2rem 2.5rem 4rem; max-width: 1200px; }

/* Header */
.hero { margin-bottom: 2.5rem; }
.hero h1 {
    font-size: 2.6rem; font-weight: 800; letter-spacing: -0.03em;
    background: linear-gradient(90deg, #c8f04a 0%, #7ef7c8 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    margin: 0 0 .25rem;
}
.hero .sub { color: #6b7280; font-size: .95rem; font-family: 'DM Mono', monospace; }

/* Metric row */
.metric-row { display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }
.metric-card {
    background: #161922; border: 1px solid #272b36;
    border-radius: 12px; padding: 1.1rem 1.5rem; flex: 1; min-width: 140px;
}
.metric-card .label { font-size: .75rem; color: #6b7280; font-family: 'DM Mono', monospace; text-transform: uppercase; letter-spacing:.08em; margin-bottom:.3rem; }
.metric-card .value { font-size: 2rem; font-weight: 800; color: #c8f04a; line-height:1; }

/* Leaderboard table */
.lb-table { width: 100%; border-collapse: collapse; }
.lb-table th {
    font-size:.72rem; font-family:'DM Mono',monospace; color:#6b7280;
    text-transform:uppercase; letter-spacing:.1em; padding:.6rem .9rem;
    border-bottom: 1px solid #272b36; text-align:left;
}
.lb-table td { padding: .8rem .9rem; border-bottom: 1px solid #1c2030; vertical-align:middle; }
.lb-table tr:last-child td { border-bottom: none; }
.lb-table tr:hover td { background: #1a1f2e; }

.rank-cell { font-size: 1.25rem; width: 2.5rem; text-align:center; }
.manager-cell { display:flex; align-items:center; gap:.75rem; }
.manager-cell img { width:36px; height:36px; border-radius:50%; border:2px solid #272b36; object-fit:cover; }
.manager-name { font-weight:600; font-size:.95rem; }
.win-pct { font-size:.8rem; color:#6b7280; font-family:'DM Mono',monospace; }

.pts-pos { color: #7ef7c8; font-family:'DM Mono',monospace; font-weight:500; }
.pts-neg { color: #f47e7e; font-family:'DM Mono',monospace; font-weight:500; }
.pts-zero{ color: #6b7280; font-family:'DM Mono',monospace; }
.mono    { font-family:'DM Mono',monospace; font-size:.88rem; }

.w-badge { background:#1d3d2c; color:#7ef7c8; border-radius:6px; padding:2px 8px; font-size:.8rem; font-family:'DM Mono',monospace; }
.l-badge { background:#3d1d1d; color:#f47e7e; border-radius:6px; padding:2px 8px; font-size:.8rem; font-family:'DM Mono',monospace; }

/* Trade detail cards */
.trade-card {
    background:#161922; border:1px solid #272b36; border-radius:12px;
    padding:1.2rem 1.4rem; margin-bottom:.8rem;
}
.trade-card .trade-header {
    display:flex; justify-content:space-between; align-items:center;
    margin-bottom:.8rem;
}
.trade-week-tag { font-family:'DM Mono',monospace; font-size:.75rem; color:#6b7280; }
.trade-sides { display:grid; grid-template-columns:1fr auto 1fr; gap:.75rem; align-items:center; }
.trade-side { background:#0d0f14; border-radius:8px; padding:.8rem 1rem; }
.side-label { font-size:.7rem; font-family:'DM Mono',monospace; color:#6b7280; text-transform:uppercase; margin-bottom:.4rem; }
.side-manager { font-weight:700; margin-bottom:.3rem; }
.side-pts { font-size:1.3rem; font-weight:800; }
.side-players { font-size:.78rem; color:#6b7280; font-family:'DM Mono',monospace; margin-top:.3rem; }
.vs-divider { text-align:center; font-weight:800; color:#272b36; font-size:1.2rem; }

.winner-side .side-pts { color: #c8f04a; }
.winner-side { border: 1px solid #2d4a20; }
.loser-side  .side-pts { color: #f47e7e; }

.section-title {
    font-size:1.1rem; font-weight:700; margin: 2rem 0 1rem;
    color: #e8eaf0; letter-spacing: -.01em;
}
.divider { border: none; border-top: 1px solid #272b36; margin: 1.5rem 0; }

/* Streamlit widget overrides */
div[data-testid="stSelectbox"] label { color: #6b7280 !important; font-family:'DM Mono',monospace; font-size:.8rem; }
div[data-testid="stExpander"] { background:#161922; border:1px solid #272b36 !important; border-radius:10px; }
</style>
""", unsafe_allow_html=True)

# ── Load data ─────────────────────────────────────────────────────────────────

with st.spinner("Loading league data…"):
    league  = get_league()
    users   = get_users()
    rosters = get_rosters()

sport = league.get("sport", "nfl")
season = league.get("season", "")
league_name = league.get("name", "Fantasy League")

current_week, playoff_start = get_season_weeks(league)
# Cap at playoff start so we only count regular season + playoffs as available
total_weeks = max(current_week - 1, 1)   # completed weeks only

user_map = build_user_map(users, rosters)

# ── Hero ──────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="hero">
  <h1>Trade Report Card</h1>
  <div class="sub">{league_name} · {season} season · {len(users)} managers · through Week {total_weeks}</div>
</div>
""", unsafe_allow_html=True)

# ── Fetch & process ───────────────────────────────────────────────────────────

with st.spinner("Pulling trades and player stats…"):
    trades       = collect_trades(total_weeks)
    player_pts   = collect_player_points(total_weeks)
    player_names = get_player_name_map()

trade_results = analyze_trades(trades, player_pts, user_map, total_weeks)
leaderboard   = build_leaderboard(trade_results, user_map)

# ── Summary metrics ───────────────────────────────────────────────────────────
total_trades = len(trade_results)
total_pts    = sum(t["winner_pts"] + t["loser_pts"] for t in trade_results)
most_active  = max(leaderboard, key=lambda x: x["trades"])["display_name"] if leaderboard else "—"

st.markdown(f"""
<div class="metric-row">
  <div class="metric-card"><div class="label">Completed Trades</div><div class="value">{total_trades}</div></div>
  <div class="metric-card"><div class="label">Pts Moved</div><div class="value">{total_pts:,.0f}</div></div>
  <div class="metric-card"><div class="label">Most Active Trader</div><div class="value" style="font-size:1.1rem;padding-top:.3rem">{most_active}</div></div>
  <div class="metric-card"><div class="label">Weeks Analyzed</div><div class="value">{total_weeks}</div></div>
</div>
""", unsafe_allow_html=True)

if total_trades == 0:
    st.info("No completed trades found yet this season. Check back once trading begins!")
    st.stop()

# ── Leaderboard ───────────────────────────────────────────────────────────────
st.markdown('<div class="section-title">🏆 Trade Leaderboard</div>', unsafe_allow_html=True)

rows_html = ""
for rank, mgr in enumerate(leaderboard, 1):
    wins   = mgr["wins"]
    losses = mgr["losses"]
    trades_played = mgr["trades"]
    win_pct = f"{wins/trades_played*100:.0f}%" if trades_played else "—"
    net = mgr["net"]
    net_class  = "pts-pos" if net > 0 else ("pts-neg" if net < 0 else "pts-zero")
    net_prefix = "+" if net > 0 else ""

    rows_html += f"""
    <tr>
      <td class="rank-cell">{rank_badge(rank)}</td>
      <td>
        <div class="manager-cell">
          <img src="{avatar_url(mgr['avatar'])}" onerror="this.src='https://sleepercdn.com/images/v2/icons/player_icons/default.jpg'"/>
          <div>
            <div class="manager-name">{mgr['display_name']}</div>
            <div class="win-pct">Win rate {win_pct}</div>
          </div>
        </div>
      </td>
      <td><span class="w-badge">{wins}W</span> <span class="l-badge">{losses}L</span></td>
      <td class="mono" style="color:#e8eaf0">{mgr['pts_gained']:,.1f}</td>
      <td class="mono" style="color:#6b7280">{mgr['pts_surrendered']:,.1f}</td>
      <td class="{net_class}">{net_prefix}{net:,.1f}</td>
    </tr>
    """

st.markdown(f"""
<table class="lb-table">
  <thead>
    <tr>
      <th></th>
      <th>Manager</th>
      <th>Record</th>
      <th>Pts Received</th>
      <th>Pts Given</th>
      <th>Net Pts</th>
    </tr>
  </thead>
  <tbody>
    {rows_html}
  </tbody>
</table>
""", unsafe_allow_html=True)

st.markdown('<hr class="divider">', unsafe_allow_html=True)

# ── Trade-by-trade detail ─────────────────────────────────────────────────────
st.markdown('<div class="section-title">🔍 Every Trade, Broken Down</div>', unsafe_allow_html=True)

# Filter by manager
all_managers = ["All managers"] + sorted({
    user_map.get(r, {}).get("display_name", f"Roster {r}")
    for t in trade_results for r in [t["winner_roster"], t["loser_roster"]]
})
selected = st.selectbox("Filter by manager", all_managers)

filtered = trade_results
if selected != "All managers":
    filtered = [
        t for t in trade_results
        if user_map.get(t["winner_roster"], {}).get("display_name") == selected
        or user_map.get(t["loser_roster"], {}).get("display_name") == selected
    ]

for t in sorted(filtered, key=lambda x: -x["trade_week"]):
    w_name = user_map.get(t["winner_roster"], {}).get("display_name", f"Roster {t['winner_roster']}")
    l_name = user_map.get(t["loser_roster"], {}).get("display_name", f"Roster {t['loser_roster']}")

    def fmt_players(pid_list):
        names = [player_names.get(str(p), player_names.get(p, str(p))) for p in pid_list]
        return " · ".join(names) if names else "—"

    w_players = fmt_players(t["winner_players"])
    l_players = fmt_players(t["loser_players"])
    margin_str = f"+{t['margin']:.1f} pts advantage" if t["margin"] else "even"

    st.markdown(f"""
    <div class="trade-card">
      <div class="trade-header">
        <span class="trade-week-tag">Week {t['trade_week']}</span>
        <span class="trade-week-tag">{margin_str}</span>
      </div>
      <div class="trade-sides">
        <div class="trade-side winner-side">
          <div class="side-label">✓ Won the trade</div>
          <div class="side-manager">{w_name}</div>
          <div class="side-pts">{t['winner_pts']:,.1f} pts</div>
          <div class="side-players">Received: {w_players}</div>
        </div>
        <div class="vs-divider">⇆</div>
        <div class="trade-side loser-side">
          <div class="side-label">✗ Lost the trade</div>
          <div class="side-manager">{l_name}</div>
          <div class="side-pts">{t['loser_pts']:,.1f} pts</div>
          <div class="side-players">Received: {l_players}</div>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown("""
<div style="margin-top:3rem; text-align:center; color:#3a3f52; font-family:'DM Mono',monospace; font-size:.75rem;">
  Data via Sleeper API · Points scored after trade date only · Refresh to update
</div>
""", unsafe_allow_html=True)
