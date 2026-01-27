import os
import sqlite3
from datetime import datetime
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify
)

# -----------------------------
# Config
# -----------------------------

DB_PATH = os.environ.get("DATABASE_PATH", "hitster.db")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-so-secret-change-me")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "0987654321")

VALID_DIFFICULTIES = ("easy", "hard", "extreme")


# -----------------------------
# DB helpers
# -----------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS players (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        email TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS rounds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question TEXT NOT NULL,
        correct_song TEXT,
        correct_artist TEXT,
        correct_year TEXT,
        status TEXT NOT NULL CHECK(status IN ('open','closed')),
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS guesses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER NOT NULL,
        round_id INTEGER NOT NULL,
        answer_song TEXT NOT NULL,
        answer_artist TEXT NOT NULL,
        answer_year TEXT NOT NULL,
        score_song INTEGER DEFAULT 0,
        score_artist INTEGER DEFAULT 0,
        score_year INTEGER DEFAULT 0,
        total_score INTEGER DEFAULT 0,
        submitted_at TEXT NOT NULL,
        UNIQUE(player_id, round_id),
        FOREIGN KEY(player_id) REFERENCES players(id),
        FOREIGN KEY(round_id) REFERENCES rounds(id)
    )
    """)

    # snapshot table to compute deltas between rounds
    cur.execute("""
    CREATE TABLE IF NOT EXISTS round_standings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER NOT NULL,
        player_id INTEGER NOT NULL,
        rank INTEGER NOT NULL,
        total_score INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(round_id) REFERENCES rounds(id),
        FOREIGN KEY(player_id) REFERENCES players(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    # default difficulty
    cur.execute("""
        INSERT OR IGNORE INTO settings(key, value)
        VALUES('difficulty', 'hard')
    """)

    conn.commit()
    conn.close()


def get_setting(cur, key, default=None):
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    if not row or row["value"] is None:
        return default
    return str(row["value"])


def set_setting(cur, key, value):
    cur.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
    if cur.rowcount == 0:
        cur.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (key, value))


def get_difficulty(cur):
    val = (get_setting(cur, "difficulty", "hard") or "hard").strip().lower()
    return val if val in VALID_DIFFICULTIES else "hard"


def game_started(cur):
    cur.execute("SELECT COUNT(*) AS cnt FROM rounds")
    return cur.fetchone()["cnt"] > 0


# -----------------------------
# Scoring
# -----------------------------

def normalize_text(s: str) -> str:
    return (s or "").strip().lower()


def try_int(s: str):
    try:
        return int(str(s).strip())
    except Exception:
        return None


def score_year_easy(correct_year: int, guess_years_by_guessid: dict):
    """
    EASY (original):
      diff 0 -> 5
      diff 1 -> 4
      diff 2 -> 3
      else:
        if all valid diffs >=3:
           closest guess(es) -> 1
           others -> 0
        otherwise 0
    """
    diffs = {gid: abs(y - correct_year) for gid, y in guess_years_by_guessid.items()}
    if not diffs:
        return {}

    min_diff = min(diffs.values())
    all_far = min_diff >= 3

    out = {}
    for gid, d in diffs.items():
        if d == 0:
            out[gid] = 5
        elif d == 1:
            out[gid] = 4
        elif d == 2:
            out[gid] = 3
        else:
            if all_far and d == min_diff:
                out[gid] = 1
            else:
                out[gid] = 0
    return out


def score_year_hard(diff: int) -> int:
    # HARD (current)
    if diff == 0:
        return 10
    if diff == 1:
        return 8
    if diff == 2:
        return 6
    if 3 <= diff <= 5:
        return 4
    if 6 <= diff <= 10:
        return 2
    return -4  # >10


def score_year_extreme(diff: int) -> int:
    # EXTREME:
    # exact -> 10
    # 1..10 -> 0
    # >10 -> -5
    if diff == 0:
        return 10
    if diff > 10:
        return -5
    return 0


def score_hitster_round(cur, round_id: int):
    """
    Scores guesses for a closed round based on the current difficulty setting.
    Song & Artist scoring (all modes):
      exact match (case-insensitive) -> 5 pts each
    """
    cur.execute("SELECT * FROM rounds WHERE id = ?", (round_id,))
    r = cur.fetchone()
    if not r:
        return

    difficulty = get_difficulty(cur)

    correct_song = normalize_text(r["correct_song"])
    correct_artist = normalize_text(r["correct_artist"])
    correct_year = try_int(r["correct_year"])

    cur.execute("SELECT * FROM guesses WHERE round_id = ?", (round_id,))
    guesses = cur.fetchall()

    # For EASY mode, prepare year diffs across valid guesses
    easy_year_scores = {}
    if difficulty == "easy" and correct_year is not None:
        guess_years = {}
        for g in guesses:
            gy = try_int(g["answer_year"])
            if gy is not None:
                guess_years[g["id"]] = gy
        easy_year_scores = score_year_easy(correct_year, guess_years)

    for g in guesses:
        gid = g["id"]

        song_guess = normalize_text(g["answer_song"])
        artist_guess = normalize_text(g["answer_artist"])
        year_guess = try_int(g["answer_year"])

        score_song = 5 if (correct_song and song_guess == correct_song) else 0
        score_artist = 5 if (correct_artist and artist_guess == correct_artist) else 0

        score_year = 0
        if correct_year is not None and year_guess is not None:
            diff = abs(year_guess - correct_year)
            if difficulty == "easy":
                score_year = easy_year_scores.get(gid, 0)
            elif difficulty == "hard":
                score_year = score_year_hard(diff)
            elif difficulty == "extreme":
                score_year = score_year_extreme(diff)
            else:
                score_year = score_year_hard(diff)

        total = score_song + score_artist + score_year

        cur.execute("""
            UPDATE guesses
            SET score_song = ?, score_artist = ?, score_year = ?, total_score = ?
            WHERE id = ?
        """, (score_song, score_artist, score_year, total, gid))


def compute_totals(cur):
    """
    Returns list of dicts: {player_id, player, total_score}
    total_score = sum(total_score) across all guesses in closed rounds.
    """
    cur.execute("""
        SELECT p.id AS player_id, p.name AS player,
               COALESCE(SUM(g.total_score), 0) AS total_score
        FROM players p
        LEFT JOIN guesses g ON g.player_id = p.id
        LEFT JOIN rounds r ON r.id = g.round_id AND r.status = 'closed'
        GROUP BY p.id, p.name
        ORDER BY total_score DESC, p.name ASC
    """)
    rows = cur.fetchall()
    out = []
    for row in rows:
        out.append({
            "player_id": row["player_id"],
            "player": row["player"],
            "total_score": int(row["total_score"]),
        })
    return out


def rank_totals(totals):
    """
    Assign ranks with "competition ranking":
      scores: 100, 100, 90 -> ranks: 1,1,3
    Returns list with added 'rank'.
    """
    ranked = []
    last_score = None
    last_rank = 0
    for idx, t in enumerate(totals, start=1):
        score = t["total_score"]
        if last_score is None or score != last_score:
            last_rank = idx
            last_score = score
        ranked.append({**t, "rank": last_rank})
    return ranked


def save_standings_snapshot(cur, round_id: int):
    now = datetime.utcnow().isoformat()
    totals = rank_totals(compute_totals(cur))
    for t in totals:
        cur.execute("""
            INSERT INTO round_standings(round_id, player_id, rank, total_score, created_at)
            VALUES(?, ?, ?, ?, ?)
        """, (round_id, t["player_id"], t["rank"], t["total_score"], now))


def latest_closed_round_ids(cur):
    cur.execute("""
        SELECT id FROM rounds
        WHERE status = 'closed'
        ORDER BY id DESC
        LIMIT 2
    """)
    ids = [row["id"] for row in cur.fetchall()]
    latest_id = ids[0] if len(ids) >= 1 else None
    prev_id = ids[1] if len(ids) >= 2 else None
    return latest_id, prev_id


def snapshot_rank_map(cur, round_id: int):
    if not round_id:
        return {}
    cur.execute("""
        SELECT player_id, rank FROM round_standings
        WHERE round_id = ?
    """, (round_id,))
    return {row["player_id"]: int(row["rank"]) for row in cur.fetchall()}


# -----------------------------
# Flask app
# -----------------------------

def create_app():
    app = Flask(__name__)
    app.secret_key = SECRET_KEY

    init_db()

    # ---------- Auth helpers ----------

    def current_player():
        pid = session.get("player_id")
        if not pid:
            return None
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM players WHERE id = ?", (pid,))
        p = cur.fetchone()
        conn.close()
        return p

    def require_player():
        return redirect(url_for("register"))

    def is_admin():
        return bool(session.get("is_admin", False))

    # ---------- Routes ----------

    @app.get("/")
    def index():
        # If player logged in -> go to play
        if session.get("player_id"):
            return redirect(url_for("game"))
        return redirect(url_for("register"))

    # ---------- Player registration ----------

    @app.route("/register", methods=["GET", "POST"])
    def register():
        error = None
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            email = (request.form.get("email") or "").strip()

            if not name:
                error = "Please enter a player name."
            else:
                conn = get_db()
                cur = conn.cursor()
                try:
                    cur.execute(
                        "INSERT INTO players(name, email) VALUES(?, ?)",
                        (name, email if email else None),
                    )
                    conn.commit()
                except sqlite3.IntegrityError:
                    # Name exists, allow "login" by that name
                    cur.execute("SELECT id FROM players WHERE name = ?", (name,))
                    row = cur.fetchone()
                    if row:
                        session["player_id"] = int(row["id"])
                        conn.close()
                        return redirect(url_for("game"))
                    error = "Player already exists."
                    conn.close()
                    return render_template("register.html", error=error)

                # set session
                cur.execute("SELECT id FROM players WHERE name = ?", (name,))
                row = cur.fetchone()
                session["player_id"] = int(row["id"])
                conn.close()
                return redirect(url_for("game"))

        return render_template("register.html", error=error)

    @app.post("/logout")
    def logout():
        session.pop("player_id", None)
        return redirect(url_for("register"))

    # ---------- Game play page ----------

    @app.route("/game", methods=["GET", "POST"])
    def game():
        p = current_player()
        if not p:
            return require_player()

        error = None
        conn = get_db()
        cur = conn.cursor()

        # current open round
        cur.execute("SELECT * FROM rounds WHERE status='open' ORDER BY id DESC LIMIT 1")
        rnd = cur.fetchone()

        already_answered = False
        if rnd:
            cur.execute("""
                SELECT 1 FROM guesses
                WHERE player_id = ? AND round_id = ?
                LIMIT 1
            """, (p["id"], rnd["id"]))
            already_answered = cur.fetchone() is not None

        if request.method == "POST":
            if not rnd:
                error = "No open round right now."
            else:
                answer_song = (request.form.get("answer_song") or "").strip()
                answer_artist = (request.form.get("answer_artist") or "").strip()
                answer_year = (request.form.get("answer_year") or "").strip()

                if not (answer_song or answer_artist or answer_year):
                    error = "Please enter at least one field."
                else:
                    try:
                        cur.execute("""
                            INSERT INTO guesses(
                                player_id, round_id,
                                answer_song, answer_artist, answer_year,
                                submitted_at
                            ) VALUES(?, ?, ?, ?, ?, ?)
                        """, (
                            p["id"], rnd["id"],
                            answer_song, answer_artist, answer_year,
                            datetime.utcnow().isoformat()
                        ))
                        conn.commit()
                        already_answered = True
                    except sqlite3.IntegrityError:
                        already_answered = True

        conn.close()
        return render_template(
            "game.html",
            player=p,
            current_round=rnd,
            already_answered=already_answered,
            error=error,
        )

    # ---------- Standings page ----------

    @app.get("/standings")
    def standings():
        return render_template("standings.html")

    # ---------- APIs ----------

    @app.get("/api/current_round")
    def api_current_round():
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM rounds WHERE status='open' ORDER BY id DESC LIMIT 1")
        rnd = cur.fetchone()
        conn.close()
        if not rnd:
            return jsonify({"round": None})
        return jsonify({
            "round": {
                "id": rnd["id"],
                "question": rnd["question"],
                "status": rnd["status"],
            }
        })

    @app.get("/api/standings")
    def api_standings():
        conn = get_db()
        cur = conn.cursor()

        totals = rank_totals(compute_totals(cur))

        latest_id, prev_id = latest_closed_round_ids(cur)
        prev_ranks = snapshot_rank_map(cur, prev_id)
        # delta: previous_rank - current_rank (positive = moved up)
        data = []
        for t in totals:
            pid = t["player_id"]
            prev_rank = prev_ranks.get(pid)
            delta = 0
            if prev_rank is not None:
                delta = int(prev_rank) - int(t["rank"])
            data.append({
                "player": t["player"],
                "score": t["total_score"],
                "rank": t["rank"],
                "delta": delta
            })

        conn.close()
        return jsonify(data)

    @app.get("/api/last_round_answers")
    def api_last_round_answers():
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT * FROM rounds
            WHERE status='closed'
            ORDER BY id DESC
            LIMIT 1
        """)
        rnd = cur.fetchone()
        if not rnd:
            conn.close()
            return jsonify({"round": None, "answers": []})

        round_id = rnd["id"]

        cur.execute("""
            SELECT
                p.name AS player,
                g.answer_song, g.answer_artist, g.answer_year,
                g.score_song, g.score_artist, g.score_year, g.total_score
            FROM players p
            LEFT JOIN guesses g
              ON g.player_id = p.id AND g.round_id = ?
            ORDER BY p.name
        """, (round_id,))
        rows = cur.fetchall()
        conn.close()

        answers = []
        for r in rows:
            answers.append({
                "player": r["player"],
                "song": r["answer_song"] or "",
                "artist": r["answer_artist"] or "",
                "year": r["answer_year"] or "",
                "score_song": int(r["score_song"]) if r["score_song"] is not None else 0,
                "score_artist": int(r["score_artist"]) if r["score_artist"] is not None else 0,
                "score_year": int(r["score_year"]) if r["score_year"] is not None else 0,
                "total_score": int(r["total_score"]) if r["total_score"] is not None else 0,
            })

        return jsonify({
            "round": {
                "id": rnd["id"],
                "question": rnd["question"],
                "correct_song": rnd["correct_song"] or "",
                "correct_artist": rnd["correct_artist"] or "",
                "correct_year": rnd["correct_year"] or "",
            },
            "answers": answers
        })

    # ---------- Admin ----------

    @app.route("/admin", methods=["GET", "POST"])
    def admin():
        error = None

        conn = get_db()
        cur = conn.cursor()

        # Handle login/logout and actions
        if request.method == "POST":
            action = (request.form.get("action") or "").strip()

            # login when not admin
            if action == "login" and not is_admin():
                pwd = (request.form.get("password") or "")
                if pwd == ADMIN_PASSWORD:
                    session["is_admin"] = True
                else:
                    error = "Wrong admin password."

            elif action == "logout_admin" and is_admin():
                session.pop("is_admin", None)

            elif not is_admin():
                error = "Admin login required."

            else:
                # admin-only actions
                if action == "set_difficulty":
                    if game_started(cur):
                        error = "Difficulty can only be changed before the first round is created."
                    else:
                        mode = (request.form.get("difficulty") or "hard").strip().lower()
                        if mode not in VALID_DIFFICULTIES:
                            mode = "hard"
                        set_setting(cur, "difficulty", mode)
                        conn.commit()

                elif action == "create_round":
                    # close any existing open round first (optional safety)
                    cur.execute("UPDATE rounds SET status='closed' WHERE status='open'")
                    question = (request.form.get("question") or "").strip() or "New round"
                    correct_song = (request.form.get("correct_song") or "").strip()
                    correct_artist = (request.form.get("correct_artist") or "").strip()
                    correct_year = (request.form.get("correct_year") or "").strip()

                    cur.execute("""
                        INSERT INTO rounds(
                            question, correct_song, correct_artist, correct_year,
                            status, created_at
                        ) VALUES(?, ?, ?, ?, 'open', ?)
                    """, (
                        question,
                        correct_song if correct_song else None,
                        correct_artist if correct_artist else None,
                        correct_year if correct_year else None,
                        datetime.utcnow().isoformat(),
                    ))
                    conn.commit()

                elif action == "set_answers":
                    # update correct answers for currently open round
                    cur.execute("SELECT * FROM rounds WHERE status='open' ORDER BY id DESC LIMIT 1")
                    rnd = cur.fetchone()
                    if not rnd:
                        error = "No open round."
                    else:
                        correct_song = (request.form.get("correct_song") or "").strip()
                        correct_artist = (request.form.get("correct_artist") or "").strip()
                        correct_year = (request.form.get("correct_year") or "").strip()

                        cur.execute("""
                            UPDATE rounds
                            SET correct_song = ?, correct_artist = ?, correct_year = ?
                            WHERE id = ?
                        """, (
                            correct_song if correct_song else None,
                            correct_artist if correct_artist else None,
                            correct_year if correct_year else None,
                            rnd["id"],
                        ))
                        conn.commit()

                elif action == "close_round":
                    # close open round, score it, save standings snapshot
                    cur.execute("SELECT * FROM rounds WHERE status='open' ORDER BY id DESC LIMIT 1")
                    rnd = cur.fetchone()
                    if not rnd:
                        error = "No open round."
                    else:
                        cur.execute("UPDATE rounds SET status='closed' WHERE id = ?", (rnd["id"],))
                        # score guesses for this round
                        score_hitster_round(cur, rnd["id"])
                        # snapshot standings for delta comparisons
                        save_standings_snapshot(cur, rnd["id"])
                        conn.commit()

                elif action == "reset_game":
                    # keep players, wipe rounds/guesses/snapshots
                    cur.execute("DELETE FROM guesses")
                    cur.execute("DELETE FROM round_standings")
                    cur.execute("DELETE FROM rounds")
                    conn.commit()

                else:
                    error = "Unknown action."

        # ---- admin page data ----
        cur.execute("SELECT * FROM rounds WHERE status='open' ORDER BY id DESC LIMIT 1")
        current_round = cur.fetchone()

        cur.execute("SELECT * FROM rounds ORDER BY id DESC LIMIT 20")
        rounds = cur.fetchall()

        current_round_players = []
        if current_round:
            cur.execute("""
                SELECT p.name, g.answer_song, g.answer_artist, g.answer_year
                FROM players p
                LEFT JOIN guesses g
                  ON p.id = g.player_id AND g.round_id = ?
                ORDER BY p.name
            """, (current_round["id"],))
            current_round_players = cur.fetchall()

        difficulty = get_difficulty(cur)
        difficulty_locked = game_started(cur)

        conn.close()

        return render_template(
            "admin.html",
            is_admin=is_admin(),
            error=error,
            current_round=current_round,
            rounds=rounds,
            current_round_players=current_round_players,
            difficulty=difficulty,
            difficulty_locked=difficulty_locked,
        )

    @app.get("/health")
    def health():
        return "ok", 200

    return app


app = create_app()

if __name__ == "__main__":
    # local run
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
