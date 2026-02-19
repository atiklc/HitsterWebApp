import os
import re
import sqlite3
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

from flask import (
    Flask, Response, render_template, request, redirect, url_for,
    session, jsonify, g
)

# ------------------------------------------------------------
# App config
# ------------------------------------------------------------
APP_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.environ.get("DB_PATH") or os.path.join(APP_DIR, "hitster.db")

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "09874563210")  # set on Render

# ------------------------------------------------------------
# Time helpers
# ------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat()

def parse_iso_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

# ------------------------------------------------------------
# Text helpers
# ------------------------------------------------------------
_ws_re = re.compile(r"\s+")
def normalize_text(s: Optional[str]) -> str:
    if not s:
        return ""
    s = str(s).strip()
    s = _ws_re.sub(" ", s)
    return s.casefold()

# ------------------------------------------------------------
# DB helpers
# ------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    if "db" not in g:
        con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON;")
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        g.db = con
    return g.db

@app.teardown_appcontext
def close_db(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()

def init_db() -> None:
    con = get_db()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS players(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rounds(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          question TEXT,
          status TEXT NOT NULL CHECK(status IN ('open','closed')),
          correct_song TEXT,
          correct_artist TEXT,
          correct_year INTEGER,
          created_at TEXT NOT NULL,
          closed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS guesses(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          round_id INTEGER NOT NULL,
          player_id INTEGER NOT NULL,
          guess_song TEXT,
          guess_artist TEXT,
          guess_year INTEGER,
          points_year INTEGER NOT NULL DEFAULT 0,
          points_song INTEGER NOT NULL DEFAULT 0,
          points_artist INTEGER NOT NULL DEFAULT 0,
          total_points INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(round_id, player_id),
          FOREIGN KEY(round_id) REFERENCES rounds(id) ON DELETE CASCADE,
          FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS standings_history(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          round_id INTEGER NOT NULL,
          player_id INTEGER NOT NULL,
          rank INTEGER NOT NULL,
          points INTEGER NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(round_id, player_id),
          FOREIGN KEY(round_id) REFERENCES rounds(id) ON DELETE CASCADE,
          FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS settings(
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )

    # defaults
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('difficulty','easy')")
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('game_status','running')")  # running|ended
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('ended_at','')")
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('auto_rounds','0')")       # 0|1
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('next_round_at','')")     # ISO UTC
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('question_prefix','Song #')")
    con.commit()

@app.before_request
def _ensure_db():
    init_db()

@app.after_request
def no_cache(resp):
    # critical: avoids “everyone sees same player” effects via caching/proxies
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["Vary"] = "Cookie"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp

def json_utf8(payload: dict | list, status: int = 200) -> Response:
    # UTF-8 + no caching (important for fast refresh pages + Arduino)
    body = json.dumps(payload, ensure_ascii=False)
    resp = app.response_class(
        response=body,
        status=status,
        mimetype="application/json; charset=utf-8",
    )
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

# ------------------------------------------------------------
# Settings helpers
# ------------------------------------------------------------
def get_setting(key: str, default: str = "") -> str:
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_setting(key: str, value: str) -> None:
    con = get_db()
    con.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    con.commit()

def game_status() -> str:
    s = (get_setting("game_status", "running") or "running").strip().lower()
    return s if s in ("running", "ended") else "running"

def ended_at() -> str:
    return get_setting("ended_at", "")

def is_game_ended() -> bool:
    return game_status() == "ended"

def set_game_ended():
    set_setting("game_status", "ended")
    set_setting("ended_at", utc_now_iso())
    set_setting("auto_rounds", "0")
    set_setting("next_round_at", "")

def set_game_running():
    set_setting("game_status", "running")
    set_setting("ended_at", "")

def auto_rounds_enabled() -> bool:
    return get_setting("auto_rounds", "0") == "1"

def next_round_at_iso() -> str:
    return get_setting("next_round_at", "")

def set_next_round_in_seconds(seconds: int):
    dt = utc_now() + timedelta(seconds=seconds)
    set_setting("next_round_at", dt.replace(microsecond=0).isoformat())

def clear_next_round():
    set_setting("next_round_at", "")

def difficulty_locked() -> bool:
    row = get_db().execute("SELECT COUNT(*) AS c FROM rounds").fetchone()
    return int(row["c"]) > 0

# ------------------------------------------------------------
# Game helpers
# ------------------------------------------------------------
def current_player():
    pid = session.get("player_id")
    if not pid:
        return None
    return get_db().execute("SELECT * FROM players WHERE id=?", (pid,)).fetchone()

def _make_auto_question(con: sqlite3.Connection) -> str:
    prefix = (get_setting("question_prefix", "Song #") or "Song #").strip()
    count = int(con.execute("SELECT COUNT(*) AS c FROM rounds").fetchone()["c"])
    n = count + 1
    if prefix.endswith("#"):
        return f"{prefix}{n}"
    if prefix.endswith("# "):
        return f"{prefix}{n}"
    if prefix.endswith(" "):
        return f"{prefix}{n}"
    return f"{prefix} {n}"

def auto_open_round_if_due() -> None:
    """
    Create a new open round ONLY when:
      - game is running
      - auto_rounds enabled
      - no open round exists
      - next_round_at is set and is due
    """
    if is_game_ended() or not auto_rounds_enabled():
        return

    due_dt = parse_iso_dt(next_round_at_iso())
    if not due_dt:
        return
    if utc_now() < due_dt:
        return

    con = get_db()
    try:
        con.execute("BEGIN IMMEDIATE")

        # re-check inside lock
        if con.execute("SELECT 1 FROM rounds WHERE status='open' LIMIT 1").fetchone():
            con.execute("COMMIT")
            return
        if (get_setting("game_status", "running") != "running") or (get_setting("auto_rounds", "0") != "1"):
            con.execute("COMMIT")
            return

        due_dt2 = parse_iso_dt(get_setting("next_round_at", ""))
        if (not due_dt2) or (utc_now() < due_dt2):
            con.execute("COMMIT")
            return

        q = _make_auto_question(con)
        con.execute(
            "INSERT INTO rounds(question,status,created_at) VALUES(?, 'open', ?)",
            (q, utc_now_iso()),
        )
        con.execute("UPDATE settings SET value='' WHERE key='next_round_at'")
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass

def get_open_round():
    con = get_db()
    row = con.execute(
        "SELECT * FROM rounds WHERE status='open' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        auto_open_round_if_due()
        row = con.execute(
            "SELECT * FROM rounds WHERE status='open' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return row

def get_last_closed_round():
    return get_db().execute(
        "SELECT * FROM rounds WHERE status='closed' ORDER BY id DESC LIMIT 1"
    ).fetchone()

# ------------------------------------------------------------
# Scoring
# ------------------------------------------------------------
def score_year(diff: int, difficulty: str) -> int:
    if difficulty == "easy":
        if diff == 0: return 5
        if diff == 1: return 4
        if diff == 2: return 3
        if 3 <= diff <= 5: return 2
        if 6 <= diff <= 10: return 1
        return 0

    if difficulty == "hard":
        if diff == 0: return 10
        if diff == 1: return 8
        if diff == 2: return 6
        if 3 <= diff <= 5: return 4
        if 6 <= diff <= 10: return 2
        return -4

    if difficulty == "extreme":
        if diff == 0: return 10
        if diff > 10: return -5
        return 0

    return 0

def score_song_artist(guess: Optional[str], correct: Optional[str]) -> int:
    if not correct:
        return 0
    return 5 if normalize_text(guess) == normalize_text(correct) else 0

def compute_and_store_round_scores(round_id: int) -> None:
    con = get_db()
    difficulty = get_setting("difficulty", "easy")

    rnd = con.execute("SELECT * FROM rounds WHERE id=?", (round_id,)).fetchone()
    if not rnd:
        return

    cy = rnd["correct_year"]
    cs = rnd["correct_song"] or ""
    ca = rnd["correct_artist"] or ""

    guesses = con.execute("SELECT * FROM guesses WHERE round_id=?", (round_id,)).fetchall()
    for g0 in guesses:
        py = 0
        if cy is not None and g0["guess_year"] is not None:
            try:
                py = score_year(abs(int(g0["guess_year"]) - int(cy)), difficulty)
            except Exception:
                py = 0

        ps = score_song_artist(g0["guess_song"], cs)
        pa = score_song_artist(g0["guess_artist"], ca)
        total = py + ps + pa

        con.execute(
            """
            UPDATE guesses
            SET points_year=?, points_song=?, points_artist=?, total_points=?, updated_at=?
            WHERE id=?
            """,
            (py, ps, pa, total, utc_now_iso(), g0["id"]),
        )
    con.commit()

def compute_standings() -> List[Dict[str, Any]]:
    con = get_db()
    rows = con.execute(
        """
        SELECT p.id AS player_id, p.name AS player,
               COALESCE(SUM(g.total_points), 0) AS points
        FROM players p
        LEFT JOIN guesses g ON g.player_id = p.id
        GROUP BY p.id
        ORDER BY points DESC, p.name COLLATE NOCASE ASC
        """
    ).fetchall()
    return [{"player_id": r["player_id"], "player": r["player"], "points": int(r["points"])} for r in rows]

def save_standings_snapshot(closed_round_id: int) -> None:
    con = get_db()
    standings = compute_standings()
    for i, s in enumerate(standings, start=1):
        con.execute(
            """
            INSERT INTO standings_history(round_id, player_id, rank, points, created_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(round_id, player_id) DO UPDATE SET
              rank=excluded.rank, points=excluded.points, created_at=excluded.created_at
            """,
            (closed_round_id, s["player_id"], i, s["points"], utc_now_iso()),
        )
    con.commit()

def get_rank_delta_for_latest() -> Dict[int, int]:
    con = get_db()
    last = con.execute(
        "SELECT id FROM rounds WHERE status='closed' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not last:
        return {}

    prev = con.execute(
        "SELECT id FROM rounds WHERE status='closed' AND id < ? ORDER BY id DESC LIMIT 1",
        (last["id"],),
    ).fetchone()

    last_rows = con.execute(
        "SELECT player_id, rank FROM standings_history WHERE round_id=?",
        (last["id"],),
    ).fetchall()
    last_rank = {r["player_id"]: r["rank"] for r in last_rows}

    if not prev:
        return {pid: 0 for pid in last_rank.keys()}

    prev_rows = con.execute(
        "SELECT player_id, rank FROM standings_history WHERE round_id=?",
        (prev["id"],),
    ).fetchall()
    prev_rank = {r["player_id"]: r["rank"] for r in prev_rows}

    delta = {}
    for pid, lr in last_rank.items():
        pr = prev_rank.get(pid, lr)
        delta[pid] = int(pr) - int(lr)  # positive => moved up
    return delta

# ------------------------------------------------------------
# Admin auth
# ------------------------------------------------------------
def require_admin():
    if not session.get("is_admin"):
        return redirect(url_for("admin_login"))
    return None

# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------
@app.get("/")
def home():
    if session.get("player_id"):
        return redirect(url_for("game"))
    return redirect(url_for("register"))

@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        name = (request.form.get("player_name") or "").strip()
        if not name:
            error = "Player name is required."
        else:
            con = get_db()
            row = con.execute(
                "SELECT id FROM players WHERE name=? COLLATE NOCASE",
                (name,),
            ).fetchone()
            if row:
                pid = int(row["id"])
            else:
                con.execute(
                    "INSERT INTO players(name, created_at) VALUES(?,?)",
                    (name, utc_now_iso()),
                )
                con.commit()
                pid = int(con.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

            session.clear()
            session["player_id"] = pid
            return redirect(url_for("game"))

    return render_template("register.html", error=error, player=current_player())

@app.get("/switch")
def switch():
    players = get_db().execute(
        "SELECT id, name FROM players ORDER BY name COLLATE NOCASE ASC"
    ).fetchall()
    return render_template("switch.html", players=players, player=current_player())

@app.post("/switch")
def switch_post():
    pid = request.form.get("player_id")
    if not pid:
        return redirect(url_for("switch"))
    row = get_db().execute("SELECT id FROM players WHERE id=?", (pid,)).fetchone()
    if not row:
        return redirect(url_for("switch"))
    session.clear()
    session["player_id"] = int(pid)
    return redirect(url_for("game"))

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("register"))

@app.get("/game")
def game():
    player = current_player()
    if not player:
        return redirect(url_for("register"))

    con = get_db()
    open_round = get_open_round()
    last_closed = get_last_closed_round()

    my_guess = None
    if open_round:
        my_guess = con.execute(
            "SELECT * FROM guesses WHERE round_id=? AND player_id=?",
            (open_round["id"], player["id"]),
        ).fetchone()

    standings = compute_standings()
    delta_map = get_rank_delta_for_latest()
    for i, s in enumerate(standings, start=1):
        s["rank"] = i
        s["delta"] = int(delta_map.get(s["player_id"], 0))

    last_results = []
    correct = None
    if last_closed:
        correct = {
            "song": last_closed["correct_song"] or "",
            "artist": last_closed["correct_artist"] or "",
            "year": last_closed["correct_year"] if last_closed["correct_year"] is not None else "",
        }
        last_results = con.execute(
            """
            SELECT p.name AS player,
                   g.guess_song, g.guess_artist, g.guess_year,
                   g.points_song, g.points_artist, g.points_year, g.total_points
            FROM guesses g
            JOIN players p ON p.id = g.player_id
            WHERE g.round_id=?
            ORDER BY p.name COLLATE NOCASE ASC
            """,
            (last_closed["id"],),
        ).fetchall()

    return render_template(
        "game.html",
        player=player,
        open_round=open_round,
        my_guess=my_guess,
        standings=standings,
        last_closed=last_closed,
        correct=correct,
        last_results=last_results,
        difficulty=get_setting("difficulty", "easy"),
        status=game_status(),
        ended_at=ended_at(),
        auto_rounds=auto_rounds_enabled(),
        next_round_at=next_round_at_iso(),
        err=request.args.get("err"),
        msg=request.args.get("msg"),
    )

@app.post("/submit")
def submit():
    player = current_player()
    if not player:
        return redirect(url_for("register"))

    if is_game_ended():
        return redirect(url_for("game", msg="ended"))

    open_round = get_open_round()
    if not open_round:
        return redirect(url_for("game", msg="no_open_round"))

    # IMPORTANT FIX:
    # Block stale pages posting into the wrong round.
    posted_round_id = (request.form.get("round_id") or "").strip()
    if not posted_round_id or str(open_round["id"]) != posted_round_id:
        return redirect(url_for("game", msg="round_changed"))

    guess_song = (request.form.get("guess_song") or "").strip()
    guess_artist = (request.form.get("guess_artist") or "").strip()
    guess_year_raw = (request.form.get("guess_year") or "").strip()

    guess_year = None
    if guess_year_raw:
        try:
            guess_year = int(guess_year_raw)
        except ValueError:
            guess_year = None

    if not (guess_song or guess_artist or guess_year_raw):
        return redirect(url_for("game", err="enter_one"))

    con = get_db()
    con.execute(
        """
        INSERT INTO guesses(round_id, player_id, guess_song, guess_artist, guess_year, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(round_id, player_id) DO UPDATE SET
          guess_song=excluded.guess_song,
          guess_artist=excluded.guess_artist,
          guess_year=excluded.guess_year,
          updated_at=excluded.updated_at
        """,
        (
            open_round["id"],
            player["id"],
            guess_song or None,
            guess_artist or None,
            guess_year,
            utc_now_iso(),
            utc_now_iso(),
        ),
    )
    con.commit()
    return redirect(url_for("game"))

# ------------------------------------------------------------
# API (player auto refresh + admin live panel)
# ------------------------------------------------------------
@app.get("/api/state")
def api_state():
    player = current_player()
    if not player:
        return jsonify({"ok": False, "needs_register": True, "server_time": utc_now_iso()}), 200

    open_round = get_open_round()
    out = {
        "ok": True,
        "player": {"id": player["id"], "name": player["name"]},
        "difficulty": get_setting("difficulty", "easy"),
        "server_time": utc_now_iso(),
        "open_round": None,
        "game_status": game_status(),
        "ended_at": ended_at(),
        "auto_rounds": auto_rounds_enabled(),
        "next_round_at": next_round_at_iso(),
    }
    if open_round:
        my_guess = get_db().execute(
            "SELECT 1 FROM guesses WHERE round_id=? AND player_id=?",
            (open_round["id"], player["id"]),
        ).fetchone()
        out["open_round"] = {
            "id": open_round["id"],
            "question": open_round["question"] or "",
            "submitted": bool(my_guess),
        }
    return jsonify(out), 200

@app.get("/api/standings")
def api_standings():
    standings = compute_standings()
    delta_map = get_rank_delta_for_latest()

    out = []
    for i, s in enumerate(standings, start=1):
        out.append({
            "rank": i,
            "player": s["player"],
            "points": s["points"],
            "delta": int(delta_map.get(s["player_id"], 0)),
        })
    return jsonify(out), 200


@app.get("/api/admin/open_round_guesses")
def api_admin_open_round_guesses():
    if not session.get("is_admin"):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    open_round = get_open_round()

    payload = {
        "ok": True,
        "game_status": game_status(),
        "ended_at": ended_at(),
        "auto_rounds": auto_rounds_enabled(),
        "next_round_at": next_round_at_iso(),
        "has_open_round": bool(open_round),
        "round": None,
        "rows": [],
    }

    con = get_db()

    # handy meta info
    payload["player_count"] = int(
        con.execute("SELECT COUNT(*) AS c FROM players").fetchone()["c"]
    )

    if not open_round:
        return jsonify(payload), 200

    payload["round"] = {"id": int(open_round["id"]), "question": open_round["question"] or ""}

    rows = con.execute(
        """
        SELECT p.name AS player,
               g.guess_song, g.guess_artist, g.guess_year,
               g.updated_at
        FROM guesses g
        JOIN players p ON p.id = g.player_id
        WHERE g.round_id=?
        ORDER BY g.updated_at DESC
        """,
        (open_round["id"],),
    ).fetchall()

    payload["rows"] = [
        {
            "player": r["player"],
            "guess_song": r["guess_song"] or "",
            "guess_artist": r["guess_artist"] or "",
            "guess_year": r["guess_year"] if r["guess_year"] is not None else "",
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]

    return jsonify(payload), 200

# ------------------------------------------------------------
# Admin pages
# ------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        pw = (request.form.get("password") or "").strip()
        if not ADMIN_PASSWORD:
            error = "ADMIN_PASSWORD is not set on the server."
        elif pw != ADMIN_PASSWORD:
            error = "Wrong password."
        else:
            session["is_admin"] = True
            return redirect(url_for("admin"))
    return render_template("admin_login.html", error=error, player=current_player())

@app.post("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("register"))

@app.route("/admin", methods=["GET", "POST"])
def admin():
    guard = require_admin()
    if guard:
        return guard

    con = get_db()
    msg = None
    err = None

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "set_difficulty":
            if difficulty_locked():
                err = "Difficulty is locked after the first round is created."
            else:
                diff = (request.form.get("difficulty") or "easy").strip().lower()
                if diff not in ("easy", "hard", "extreme"):
                    diff = "easy"
                set_setting("difficulty", diff)
                msg = f"Difficulty set to {diff}."

        elif action == "set_prefix":
            if difficulty_locked():
                # safe to allow, but keep consistent; you can remove this restriction if you want
                pass
            prefix = (request.form.get("question_prefix") or "Song #").strip()
            if not prefix:
                prefix = "Song #"
            set_setting("question_prefix", prefix)
            msg = "Question prefix updated."

        elif action == "start_game":
            if is_game_ended():
                set_game_running()
            set_setting("auto_rounds", "1")

            # if no open round, open immediately by scheduling "now"
            if not con.execute("SELECT 1 FROM rounds WHERE status='open' LIMIT 1").fetchone():
                set_setting("next_round_at", utc_now_iso())
                auto_open_round_if_due()
            msg = "Game started. Auto-rounds enabled."

        elif action == "stop_auto":
            set_setting("auto_rounds", "0")
            clear_next_round()
            msg = "Auto-rounds disabled."

        elif action == "set_answers":
            if is_game_ended():
                err = "Game is ended. No changes allowed."
            else:
                open_round = get_open_round()
                if not open_round:
                    err = "No open round."
                else:
                    cs = (request.form.get("correct_song") or "").strip() or None
                    ca = (request.form.get("correct_artist") or "").strip() or None
                    cy_raw = (request.form.get("correct_year") or "").strip()

                    cy = None
                    if cy_raw:
                        try:
                            cy = int(cy_raw)
                        except ValueError:
                            cy = None

                    con.execute(
                        """
                        UPDATE rounds
                        SET correct_song=?, correct_artist=?, correct_year=?
                        WHERE id=? AND status='open'
                        """,
                        (cs, ca, cy, open_round["id"]),
                    )
                    con.commit()
                    msg = "Correct answers updated."

        elif action == "close_round":
            if is_game_ended():
                err = "Game is ended. No changes allowed."
            else:
                open_round = get_open_round()
                if not open_round:
                    err = "No open round."
                else:
                    rid = int(open_round["id"])
                    compute_and_store_round_scores(rid)
                    con.execute(
                        "UPDATE rounds SET status='closed', closed_at=? WHERE id=?",
                        (utc_now_iso(), rid),
                    )
                    con.commit()
                    save_standings_snapshot(rid)

                    if auto_rounds_enabled() and (game_status() == "running"):
                        set_next_round_in_seconds(5)
                        msg = "Round closed & scored. Next round opens in 5 seconds."
                    else:
                        msg = "Round closed & scored."

        elif action == "end_game":
            if is_game_ended():
                msg = "Game is already ended."
            else:
                open_round = con.execute(
                    "SELECT * FROM rounds WHERE status='open' ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if open_round:
                    rid = int(open_round["id"])
                    compute_and_store_round_scores(rid)
                    con.execute(
                        "UPDATE rounds SET status='closed', closed_at=? WHERE id=?",
                        (utc_now_iso(), rid),
                    )
                    con.commit()
                    save_standings_snapshot(rid)
                set_game_ended()
                msg = "Game ended. Submissions locked."

        elif action == "reset_game":
            con.execute("DELETE FROM guesses")
            con.execute("DELETE FROM standings_history")
            con.execute("DELETE FROM rounds")
            con.execute("UPDATE settings SET value='easy' WHERE key='difficulty'")
            con.execute("UPDATE settings SET value='running' WHERE key='game_status'")
            con.execute("UPDATE settings SET value='' WHERE key='ended_at'")
            con.execute("UPDATE settings SET value='0' WHERE key='auto_rounds'")
            con.execute("UPDATE settings SET value='' WHERE key='next_round_at'")
            con.execute("UPDATE settings SET value='Song #' WHERE key='question_prefix'")
            con.commit()
            msg = "Game reset. Players kept; rounds/guesses cleared."

    # useful: if the 5s timer is due, open a round when admin loads page
    auto_open_round_if_due()
    open_round = get_open_round()

    open_round = get_open_round()
    players = con.execute(
        "SELECT id, name, created_at FROM players ORDER BY name COLLATE NOCASE"
    ).fetchall()

    live_rows = []
    if open_round:
        live_rows = con.execute(
            """
            SELECT p.name AS player, g.guess_song, g.guess_artist, g.guess_year, g.updated_at
            FROM guesses g
            JOIN players p ON p.id=g.player_id
            WHERE g.round_id=?
            ORDER BY g.updated_at DESC, p.name COLLATE NOCASE ASC
            """,
            (open_round["id"],),
        ).fetchall()

    return render_template(
        "admin.html",
        msg=msg,
        err=err,
        open_round=open_round,
        current_round=open_round,  # template compatibility
        players=players,
        live_rows=live_rows,
        difficulty=get_setting("difficulty", "easy"),
        locked=difficulty_locked(),
        status=game_status(),
        ended_at=ended_at(),
        auto_rounds=auto_rounds_enabled(),
        next_round_at=next_round_at_iso(),
        question_prefix=get_setting("question_prefix", "Song #"),
        auto_delay=5,
        player=current_player(),
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
