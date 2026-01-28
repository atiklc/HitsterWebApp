import os
import sqlite3
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, abort, flash
)

# ----------------------------
# App config
# ----------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "0987654321")

DB_PATH = os.environ.get("DB_PATH", "hitster.db")


# ----------------------------
# DB helpers
# ----------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS players (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT,
        name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS rounds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question TEXT,
        correct_song TEXT,
        correct_artist TEXT,
        correct_year INTEGER,
        status TEXT NOT NULL, -- 'open' or 'closed'
        created_at TEXT NOT NULL,
        closed_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS guesses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER NOT NULL,
        player_id INTEGER NOT NULL,
        guess_song TEXT,
        guess_artist TEXT,
        guess_year INTEGER,
        submitted_at TEXT NOT NULL,
        points_song INTEGER NOT NULL DEFAULT 0,
        points_artist INTEGER NOT NULL DEFAULT 0,
        points_year INTEGER NOT NULL DEFAULT 0,
        total_points INTEGER NOT NULL DEFAULT 0,
        UNIQUE(round_id, player_id),
        FOREIGN KEY(round_id) REFERENCES rounds(id),
        FOREIGN KEY(player_id) REFERENCES players(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS standings_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER NOT NULL,
        player_id INTEGER NOT NULL,
        rank INTEGER NOT NULL,
        total_points INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(round_id, player_id),
        FOREIGN KEY(round_id) REFERENCES rounds(id),
        FOREIGN KEY(player_id) REFERENCES players(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)

    # default difficulty if missing
    cur.execute("SELECT value FROM settings WHERE key='difficulty'")
    if cur.fetchone() is None:
        cur.execute("INSERT INTO settings(key,value) VALUES('difficulty','easy')")

    con.commit()
    con.close()

init_db()


# ----------------------------
# Auth helpers
# ----------------------------
def require_player(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("player_id"):
            return redirect(url_for("register"))
        return fn(*args, **kwargs)
    return wrapper

def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper


# ----------------------------
# Game logic
# ----------------------------
def get_setting(key: str, default: str = "") -> str:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    con.close()
    return row["value"] if row else default

def set_setting(key: str, value: str):
    con = db()
    cur = con.cursor()
    cur.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    con.commit()
    con.close()

def get_open_round():
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM rounds WHERE status='open' ORDER BY id DESC LIMIT 1")
    r = cur.fetchone()
    con.close()
    return r

def round_count():
    con = db()
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM rounds")
    cnt = cur.fetchone()["cnt"]
    con.close()
    return cnt

def parse_int(v):
    try:
        v = str(v).strip()
        if v == "":
            return None
        return int(v)
    except Exception:
        return None

def norm_text(s: str) -> str:
    return (s or "").strip().casefold()

def score_year_hard(diff: int) -> int:
    # a. Exact answer - 10pt
    # b. +/- 1 year - 8pt
    # c. +/- 2 years - 6pt
    # d. +/- 3-5 years - 4 pt
    # e. +/- 6-10 years - 2 pt
    # f. +/- >10 years - (-4) pt
    if diff == 0: return 10
    if diff == 1: return 8
    if diff == 2: return 6
    if 3 <= diff <= 5: return 4
    if 6 <= diff <= 10: return 2
    return -4

def score_year_extreme(diff: int) -> int:
    # exact=10
    # wrong >10 => -5 penalty
    # else 0
    if diff == 0:
        return 10
    if diff > 10:
        return -5
    return 0

def score_year_easy_individual(diff: int) -> int:
    # base mapping without "closest" rule
    if diff == 0: return 5
    if diff == 1: return 4
    if diff == 2: return 3
    return 0

def compute_standings():
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT p.id AS player_id, p.name AS player,
               COALESCE(SUM(g.total_points),0) AS points
        FROM players p
        LEFT JOIN guesses g ON g.player_id=p.id
        LEFT JOIN rounds r ON r.id=g.round_id AND r.status='closed'
        GROUP BY p.id
        ORDER BY points DESC, p.name COLLATE NOCASE ASC
    """)
    rows = cur.fetchall()
    con.close()

    # dense ranking
    standings = []
    last_pts = None
    rank = 0
    for i, row in enumerate(rows):
        pts = int(row["points"])
        if last_pts is None or pts != last_pts:
            rank = rank + 1
            last_pts = pts
        standings.append({"rank": rank, "player": row["player"], "player_id": row["player_id"], "points": pts})
    return standings

def get_last_two_snapshots():
    con = db()
    cur = con.cursor()
    cur.execute("SELECT id, round_id FROM rounds WHERE status='closed' ORDER BY id DESC LIMIT 2")
    rr = cur.fetchall()
    con.close()
    if len(rr) == 0:
        return None, None
    if len(rr) == 1:
        return rr[0]["id"], None
    return rr[0]["id"], rr[1]["id"]

def compute_position_deltas(standings):
    last_closed_round_id, prev_closed_round_id = get_last_two_snapshots()
    if not last_closed_round_id:
        for s in standings:
            s["delta"] = 0
        return standings

    con = db()
    cur = con.cursor()

    cur.execute("SELECT player_id, rank FROM standings_snapshots WHERE round_id=?", (last_closed_round_id,))
    last_map = {r["player_id"]: r["rank"] for r in cur.fetchall()}

    prev_map = {}
    if prev_closed_round_id:
        cur.execute("SELECT player_id, rank FROM standings_snapshots WHERE round_id=?", (prev_closed_round_id,))
        prev_map = {r["player_id"]: r["rank"] for r in cur.fetchall()}

    con.close()

    out = []
    for s in standings:
        pid = s["player_id"]
        cur_rank = last_map.get(pid, s["rank"])
        prev_rank = prev_map.get(pid)
        delta = 0
        if prev_rank is not None:
            delta = prev_rank - cur_rank  # + means went up
        out.append({**s, "rank": cur_rank, "delta": delta})
    out.sort(key=lambda x: (x["rank"], x["player"].casefold()))
    return out

def save_snapshot_for_round(round_id: int):
    standings = compute_standings()
    standings = compute_position_deltas(standings)

    con = db()
    cur = con.cursor()
    now = utc_now_iso()

    for s in standings:
        cur.execute("""
            INSERT INTO standings_snapshots(round_id, player_id, rank, total_points, created_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(round_id, player_id) DO UPDATE SET
              rank=excluded.rank, total_points=excluded.total_points, created_at=excluded.created_at
        """, (round_id, s["player_id"], s["rank"], s["points"], now))

    con.commit()
    con.close()

def get_last_closed_round_with_guesses():
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM rounds WHERE status='closed' ORDER BY id DESC LIMIT 1")
    rnd = cur.fetchone()
    if not rnd:
        con.close()
        return None, []

    cur.execute("""
        SELECT p.name AS player, g.guess_song, g.guess_artist, g.guess_year,
               g.points_song, g.points_artist, g.points_year, g.total_points
        FROM guesses g
        JOIN players p ON p.id=g.player_id
        WHERE g.round_id=?
        ORDER BY p.name COLLATE NOCASE ASC
    """, (rnd["id"],))
    guesses = cur.fetchall()
    con.close()
    return rnd, guesses


# ----------------------------
# Routes
# ----------------------------
@app.get("/")
def home():
    # Main entry goes to game page
    return redirect(url_for("game"))

@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        name = (request.form.get("name") or "").strip()
        session["player_id"] = new_player_id
        session["player_name"] = player_name
        return redirect(url_for("game"))
        
        if not name:
            error = "Player name is required."
        elif len(name) > 30:
            error = "Player name is too long (max 30)."
        else:
            con = db()
            cur = con.cursor()
            cur.execute("SELECT id FROM players WHERE name=? COLLATE NOCASE", (name,))
            row = cur.fetchone()
            if row:
                player_id = row["id"]
                # update email if provided
                if email:
                    cur.execute("UPDATE players SET email=? WHERE id=?", (email, player_id))
            else:
                cur.execute(
                    "INSERT INTO players(email,name,created_at) VALUES(?,?,?)",
                    (email, name, utc_now_iso())
                )
                player_id = cur.lastrowid

            con.commit()
            con.close()

            session["player_id"] = int(player_id)
            return redirect(url_for("game"))

    return render_template("register.html", error=error)

@app.get("/logout")
def logout():
    session.pop("player_id", None)
    session.pop("is_admin", None)
    return redirect(url_for("register"))

@app.get("/game")
def game():
    player_id = session.get("player_id")
    if not player_id:
        return redirect(url_for("register"))

    # fetch player + open round safely...
    open_round = get_open_round()  # your function
    return render_template("game.html", open_round=open_round)
    standings = compute_position_deltas(compute_standings())
    last_round, last_guesses = get_last_closed_round_with_guesses()

    # Did current player submit to open round?
    my_submitted = False
    my_guess = None
    if open_round:
        con = db()
        cur = con.cursor()
        cur.execute("SELECT * FROM guesses WHERE round_id=? AND player_id=?", (open_round["id"], session["player_id"]))
        g = cur.fetchone()
        con.close()
        if g:
            my_submitted = True
            my_guess = g

    return render_template(
        "game.html",
        open_round=open_round,
        my_submitted=my_submitted,
        my_guess=my_guess,
        standings=standings,
        last_round=last_round,
        last_guesses=last_guesses
    )

@app.post("/submit")
@require_player
def submit():
    open_round = get_open_round()
    if not open_round:
        flash("No open round right now.", "warning")
        return redirect(url_for("game"))

    song = (request.form.get("guess_song") or "").strip()
    artist = (request.form.get("guess_artist") or "").strip()
    year = parse_int(request.form.get("guess_year"))

    # require at least one field
    if not song and not artist and year is None:
        flash("Please enter at least one field.", "warning")
        return redirect(url_for("game"))

    con = db()
    cur = con.cursor()
    try:
        cur.execute("""
            INSERT INTO guesses(round_id, player_id, guess_song, guess_artist, guess_year, submitted_at)
            VALUES(?,?,?,?,?,?)
        """, (open_round["id"], session["player_id"], song, artist, year, utc_now_iso()))
        con.commit()
        flash("Submitted ✅", "success")
    except sqlite3.IntegrityError:
        # already submitted -> update instead
        cur.execute("""
            UPDATE guesses
            SET guess_song=?, guess_artist=?, guess_year=?, submitted_at=?
            WHERE round_id=? AND player_id=?
        """, (song, artist, year, utc_now_iso(), open_round["id"], session["player_id"]))
        con.commit()
        flash("Updated ✅", "success")
    finally:
        con.close()

    return redirect(url_for("game"))

@app.get("/standings")
def standings_page():
    standings = compute_position_deltas(compute_standings())
    last_round, last_guesses = get_last_closed_round_with_guesses()
    return render_template("standings.html", standings=standings, last_round=last_round, last_guesses=last_guesses)

# ----- Admin auth -----
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        pw = request.form.get("password") or ""
        if pw == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin"))
        error = "Wrong password."
    return render_template("base.html", content_only=True, title="Admin login",
                           inner_template="",
                           error=error, show_login=True)

@app.get("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))

@app.route("/admin", methods=["GET", "POST"])
@require_admin
def admin():
    # Difficulty lock: once any round exists, lock it
    locked = round_count() > 0
    difficulty = get_setting("difficulty", "easy")

    con = db()
    cur = con.cursor()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "set_difficulty":
            if locked:
                flash("Difficulty is locked once the game starts.", "warning")
            else:
                diff = request.form.get("difficulty") or "easy"
                if diff not in ("easy", "hard", "extreme"):
                    diff = "easy"
                set_setting("difficulty", diff)
                difficulty = diff
                flash("Difficulty saved.", "success")

        elif action == "create_round":
            q = (request.form.get("question") or "").strip()
            # close any old open rounds (safety)
            cur.execute("UPDATE rounds SET status='closed', closed_at=? WHERE status='open'", (utc_now_iso(),))
            cur.execute("""
                INSERT INTO rounds(question, correct_song, correct_artist, correct_year, status, created_at)
                VALUES(?,?,?,?, 'open', ?)
            """, (
                q,
                (request.form.get("correct_song") or "").strip(),
                (request.form.get("correct_artist") or "").strip(),
                parse_int(request.form.get("correct_year")),
                utc_now_iso()
            ))
            con.commit()
            flash("Round created & opened.", "success")

        elif action == "set_answers":
            r = get_open_round()
            if not r:
                flash("No open round.", "warning")
            else:
                cur.execute("""
                    UPDATE rounds
                    SET correct_song=?, correct_artist=?, correct_year=?
                    WHERE id=?
                """, (
                    (request.form.get("correct_song") or "").strip(),
                    (request.form.get("correct_artist") or "").strip(),
                    parse_int(request.form.get("correct_year")),
                    r["id"]
                ))
                con.commit()
                flash("Correct answers updated.", "success")

        elif action == "close_round":
            r = get_open_round()
            if not r:
                flash("No open round.", "warning")
            else:
                # Score this round
                cur.execute("UPDATE rounds SET status='closed', closed_at=? WHERE id=?", (utc_now_iso(), r["id"]))
                con.commit()
                score_round(r["id"])
                save_snapshot_for_round(r["id"])
                flash("Round closed & scored.", "success")

        elif action == "reset_game_keep_players":
            cur.execute("DELETE FROM guesses")
            cur.execute("DELETE FROM standings_snapshots")
            cur.execute("DELETE FROM rounds")
            con.commit()
            # unlock difficulty again
            flash("Game reset (players kept).", "success")

        elif action == "full_reset":
            cur.execute("DELETE FROM guesses")
            cur.execute("DELETE FROM standings_snapshots")
            cur.execute("DELETE FROM rounds")
            cur.execute("DELETE FROM players")
            con.commit()
            flash("Full reset done.", "success")

    # Load view data
    cur.execute("SELECT * FROM rounds ORDER BY id DESC LIMIT 20")
    rounds = cur.fetchall()

    cur.execute("SELECT * FROM players ORDER BY name COLLATE NOCASE ASC")
    players = cur.fetchall()

    current_round = get_open_round()

    # Who submitted to the current open round?
    submissions = []
    if current_round:
        cur.execute("""
            SELECT p.name AS player, g.submitted_at, g.guess_song, g.guess_artist, g.guess_year
            FROM guesses g
            JOIN players p ON p.id=g.player_id
            WHERE g.round_id=?
            ORDER BY g.submitted_at DESC
        """, (current_round["id"],))
        submissions = cur.fetchall()

    con.close()

    return render_template(
        "admin.html",
        rounds=rounds,
        players=players,
        current_round=current_round,
        submissions=submissions,
        difficulty=difficulty,
        difficulty_locked=locked
    )


def score_round(round_id: int):
    con = db()
    cur = con.cursor()

    cur.execute("SELECT * FROM rounds WHERE id=?", (round_id,))
    r = cur.fetchone()
    if not r:
        con.close()
        return

    correct_song = (r["correct_song"] or "").strip()
    correct_artist = (r["correct_artist"] or "").strip()
    correct_year = r["correct_year"]
    difficulty = get_setting("difficulty", "easy")

    cur.execute("SELECT * FROM guesses WHERE round_id=?", (round_id,))
    guesses = cur.fetchall()

    # For "easy" closest-rule we need to know if anyone is within 0-2
    diffs = []
    if correct_year is not None:
        for g in guesses:
            if g["guess_year"] is not None:
                diffs.append(abs(int(g["guess_year"]) - int(correct_year)))
    min_diff = min(diffs) if diffs else None

    for g in guesses:
        pts_song = 0
        pts_artist = 0
        pts_year = 0

        if correct_song and norm_text(g["guess_song"]) == norm_text(correct_song):
            pts_song = 5
        if correct_artist and norm_text(g["guess_artist"]) == norm_text(correct_artist):
            pts_artist = 5

        if correct_year is not None and g["guess_year"] is not None:
            diff = abs(int(g["guess_year"]) - int(correct_year))

            if difficulty == "hard":
                pts_year = score_year_hard(diff)

            elif difficulty == "extreme":
                pts_year = score_year_extreme(diff)

            else:  # easy
                # normal mapping if someone is close (0-2)
                if min_diff is not None and min_diff <= 2:
                    pts_year = score_year_easy_individual(diff)
                else:
                    # nobody within 0-2 -> closest gets 1, others 0
                    if min_diff is not None and diff == min_diff:
                        pts_year = 1
                    else:
                        pts_year = 0

        total = int(pts_song) + int(pts_artist) + int(pts_year)

        cur.execute("""
            UPDATE guesses
            SET points_song=?, points_artist=?, points_year=?, total_points=?
            WHERE id=?
        """, (pts_song, pts_artist, pts_year, total, g["id"]))

    con.commit()
    con.close()


# ----------------------------
# JSON APIs (Arduino / auto-refresh)
# ----------------------------
@app.get("/api/state")
def api_state():
    r = get_open_round()
    return jsonify({
        "open_round_id": int(r["id"]) if r else None,
        "open_round_question": r["question"] if r else None,
    })

@app.get("/api/standings")
def api_standings():
    standings = compute_position_deltas(compute_standings())
    last_round, last_guesses = get_last_closed_round_with_guesses()

    payload = {
        "standings": [{"rank": s["rank"], "player": s["player"], "points": s["points"], "delta": s["delta"]} for s in standings],
        "last_round": None,
        "last_round_guesses": []
    }
    if last_round:
        payload["last_round"] = {
            "id": int(last_round["id"]),
            "question": last_round["question"],
            "correct_song": last_round["correct_song"],
            "correct_artist": last_round["correct_artist"],
            "correct_year": last_round["correct_year"],
        }
        payload["last_round_guesses"] = [
            {
                "player": g["player"],
                "guess_song": g["guess_song"],
                "guess_artist": g["guess_artist"],
                "guess_year": g["guess_year"],
                "points_song": g["points_song"],
                "points_artist": g["points_artist"],
                "points_year": g["points_year"],
                "total_points": g["total_points"],
            }
            for g in last_guesses
        ]

    # ensure UTF-8 content in JSON
    resp = app.response_class(
        response=jsonify(payload).get_data(as_text=False),
        status=200,
        mimetype="application/json"
    )
    return resp


if __name__ == "__main__":
    # local dev only
    app.run(host="0.0.0.0", port=10000, debug=True)
