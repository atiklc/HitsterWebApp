import os
import sqlite3
from datetime import datetime, timezone

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, abort
)

# -----------------------------
# App setup
# -----------------------------
app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "0987654321")  # change on Render!

DB_PATH = os.environ.get("DB_PATH", "hitster.db")


# -----------------------------
# Helpers
# -----------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db():
    with db_connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS players (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              email TEXT,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rounds (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              question TEXT,
              status TEXT NOT NULL CHECK(status IN ('open','closed')),
              correct_song TEXT,
              correct_artist TEXT,
              correct_year INTEGER,
              created_at TEXT NOT NULL,
              closed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS guesses (
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

            -- Snapshots after each closed round for delta (position change)
            CREATE TABLE IF NOT EXISTS standings_history (
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

            -- Game settings (difficulty only). NOT used for current player.
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            """
        )

        # Default difficulty
        cur = con.execute("SELECT value FROM settings WHERE key='difficulty'")
        if cur.fetchone() is None:
            con.execute("INSERT INTO settings(key,value) VALUES('difficulty','easy')")


@app.before_request
def _ensure_db():
    init_db()


def get_setting(key: str, default: str = "") -> str:
    with db_connect() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db_connect() as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def difficulty_locked() -> bool:
    with db_connect() as con:
        cnt = con.execute("SELECT COUNT(*) AS c FROM rounds").fetchone()["c"]
        return cnt > 0


def current_player():
    pid = session.get("player_id")
    if not pid:
        return None
    with db_connect() as con:
        return con.execute("SELECT * FROM players WHERE id=?", (pid,)).fetchone()


def get_open_round():
    with db_connect() as con:
        return con.execute(
            "SELECT * FROM rounds WHERE status='open' ORDER BY id DESC LIMIT 1"
        ).fetchone()


def get_last_closed_round():
    with db_connect() as con:
        return con.execute(
            "SELECT * FROM rounds WHERE status='closed' ORDER BY id DESC LIMIT 1"
        ).fetchone()


def normalize_text(s: str) -> str:
    # Simple normalization for matching (case-insensitive, trimmed)
    return (s or "").strip().lower()


def score_year(diff: int, difficulty: str) -> int:
    # diff = abs(guess - correct)
    if difficulty == "easy":
        # 5..1, no negatives
        if diff == 0:
            return 5
        if diff == 1:
            return 4
        if diff == 2:
            return 3
        if 3 <= diff <= 5:
            return 2
        if 6 <= diff <= 10:
            return 1
        return 0

    if difficulty == "hard":
        # Your current (annoying-but-precise) system
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

    if difficulty == "extreme":
        # Only exact answer counts. Big miss penalty.
        if diff == 0:
            return 10
        if diff > 10:
            return -5
        return 0

    # fallback
    return 0


def score_song_artist(guess: str, correct: str) -> int:
    # 5 pts for exact match if correct is provided
    if not correct:
        return 0
    return 5 if normalize_text(guess) == normalize_text(correct) else 0


def compute_and_store_round_scores(round_id: int):
    """Compute scores for guesses in this round (only when closing the round)."""
    difficulty = get_setting("difficulty", "easy")

    with db_connect() as con:
        rnd = con.execute("SELECT * FROM rounds WHERE id=?", (round_id,)).fetchone()
        if not rnd:
            return

        cy = rnd["correct_year"]
        cs = rnd["correct_song"] or ""
        ca = rnd["correct_artist"] or ""

        guesses = con.execute(
            "SELECT * FROM guesses WHERE round_id=?", (round_id,)
        ).fetchall()

        for g in guesses:
            py = 0
            if cy is not None and g["guess_year"] is not None:
                py = score_year(abs(int(g["guess_year"]) - int(cy)), difficulty)

            ps = score_song_artist(g["guess_song"] or "", cs)
            pa = score_song_artist(g["guess_artist"] or "", ca)

            total = py + ps + pa
            con.execute(
                """
                UPDATE guesses
                SET points_year=?, points_song=?, points_artist=?, total_points=?, updated_at=?
                WHERE id=?
                """,
                (py, ps, pa, total, utc_now_iso(), g["id"]),
            )


def compute_standings():
    """Return standings list: [{player_id,name,points}] sorted."""
    with db_connect() as con:
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

    standings = []
    for r in rows:
        standings.append(
            {
                "player_id": r["player_id"],
                "player": r["player"],
                "points": int(r["points"]),
            }
        )
    return standings


def save_standings_snapshot(closed_round_id: int):
    """Save rank snapshot after a round is closed (for delta calculation)."""
    standings = compute_standings()
    with db_connect() as con:
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


def get_rank_delta_for_latest():
    """Return dict {player_id: delta} comparing last two snapshots."""
    with db_connect() as con:
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


def require_admin():
    if not session.get("is_admin"):
        abort(403)


# -----------------------------
# Routes (public)
# -----------------------------
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
        email = (request.form.get("email") or "").strip()

        if not name:
            error = "Player name is required."
        else:
            with db_connect() as con:
                # If name exists, treat as login (simple party-game behavior)
                row = con.execute(
                    "SELECT id FROM players WHERE name=? COLLATE NOCASE", (name,)
                ).fetchone()

                if row:
                    pid = row["id"]
                    # Optionally update email if provided
                    if email:
                        con.execute("UPDATE players SET email=? WHERE id=?", (email, pid))
                else:
                    con.execute(
                        "INSERT INTO players(name,email,created_at) VALUES(?,?,?)",
                        (name, email or None, utc_now_iso()),
                    )
                    pid = con.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

            session.clear()
            session["player_id"] = int(pid)
            return redirect(url_for("game"))

    return render_template("register.html", error=error, player=current_player())


@app.get("/switch")
def switch():
    # Choose an existing player on this device
    with db_connect() as con:
        players = con.execute(
            "SELECT id, name FROM players ORDER BY name COLLATE NOCASE ASC"
        ).fetchall()
    return render_template("switch.html", players=players, player=current_player())


@app.post("/switch")
def switch_post():
    pid = request.form.get("player_id")
    if not pid:
        return redirect(url_for("switch"))

    with db_connect() as con:
        row = con.execute("SELECT id FROM players WHERE id=?", (pid,)).fetchone()
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

    open_round = get_open_round()
    last_closed = get_last_closed_round()

    # Player's submission status for open round
    my_guess = None
    if open_round:
        with db_connect() as con:
            my_guess = con.execute(
                "SELECT * FROM guesses WHERE round_id=? AND player_id=?",
                (open_round["id"], player["id"]),
            ).fetchone()

    # Standings and deltas
    standings = compute_standings()
    delta_map = get_rank_delta_for_latest()
    for i, s in enumerate(standings, start=1):
        s["rank"] = i
        s["delta"] = int(delta_map.get(s["player_id"], 0))

    # Last closed round results table (answers + correct)
    last_results = []
    correct = None
    if last_closed:
        correct = {
            "song": last_closed["correct_song"] or "",
            "artist": last_closed["correct_artist"] or "",
            "year": last_closed["correct_year"] if last_closed["correct_year"] is not None else "",
        }
        with db_connect() as con:
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

    difficulty = get_setting("difficulty", "easy")

    return render_template(
        "game.html",
        player=player,
        open_round=open_round,
        my_guess=my_guess,
        standings=standings,
        last_closed=last_closed,
        correct=correct,
        last_results=last_results,
        difficulty=difficulty,
    )


@app.post("/submit")
def submit():
    player = current_player()
    if not player:
        return redirect(url_for("register"))

    open_round = get_open_round()
    if not open_round:
        return redirect(url_for("game"))

    guess_song = (request.form.get("guess_song") or "").strip()
    guess_artist = (request.form.get("guess_artist") or "").strip()
    guess_year_raw = (request.form.get("guess_year") or "").strip()
    guess_year = None
    if guess_year_raw:
        try:
            guess_year = int(guess_year_raw)
        except ValueError:
            guess_year = None

    # Require at least ONE field
    if not (guess_song or guess_artist or guess_year_raw):
        # Keep it simple: redirect with query flag
        return redirect(url_for("game", err="enter_one"))

    with db_connect() as con:
        # Upsert so players can correct their entry while round is open
        con.execute(
            """
            INSERT INTO guesses(
              round_id, player_id, guess_song, guess_artist, guess_year,
              created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?)
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

    return redirect(url_for("game"))


# -----------------------------
# API (for auto-refresh + Arduino)
# -----------------------------
@app.get("/api/state")
def api_state():
    player = current_player()
    if not player:
        return jsonify({"ok": False, "needs_register": True}), 200

    open_round = get_open_round()
    data = {
        "ok": True,
        "player": {"id": player["id"], "name": player["name"]},
        "open_round": None,
        "difficulty": get_setting("difficulty", "easy"),
        "server_time": utc_now_iso(),
    }

    if open_round:
        with db_connect() as con:
            my_guess = con.execute(
                "SELECT * FROM guesses WHERE round_id=? AND player_id=?",
                (open_round["id"], player["id"]),
            ).fetchone()
        data["open_round"] = {
            "id": open_round["id"],
            "question": open_round["question"] or "",
            "submitted": bool(my_guess),
        }

    return jsonify(data), 200


@app.get("/api/standings")
def api_standings():
    standings = compute_standings()
    delta_map = get_rank_delta_for_latest()

    out = []
    for i, s in enumerate(standings, start=1):
        out.append(
            {
                "rank": i,
                "player": s["player"],
                "points": s["points"],
                "delta": int(delta_map.get(s["player_id"], 0)),
            }
        )
    # ensure utf-8 characters survive nicely
    return app.response_class(
        response=jsonify(out).get_data(as_text=True),
        status=200,
        mimetype="application/json; charset=utf-8",
    )


@app.get("/api/last_round")
def api_last_round():
    last_closed = get_last_closed_round()
    if not last_closed:
        return jsonify({"ok": True, "has_round": False}), 200

    with db_connect() as con:
        rows = con.execute(
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

    payload = {
        "ok": True,
        "has_round": True,
        "round": {
            "id": last_closed["id"],
            "question": last_closed["question"] or "",
            "correct_song": last_closed["correct_song"] or "",
            "correct_artist": last_closed["correct_artist"] or "",
            "correct_year": last_closed["correct_year"],
            "closed_at": last_closed["closed_at"] or "",
        },
        "guesses": [
            {
                "player": r["player"],
                "guess_song": r["guess_song"] or "",
                "guess_artist": r["guess_artist"] or "",
                "guess_year": r["guess_year"] if r["guess_year"] is not None else "",
                "points_song": int(r["points_song"]),
                "points_artist": int(r["points_artist"]),
                "points_year": int(r["points_year"]),
                "total_points": int(r["total_points"]),
            }
            for r in rows
        ],
    }
    return jsonify(payload), 200


# -----------------------------
# Admin
# -----------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        pw = request.form.get("password") or ""
        if pw == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin"))
        error = "Wrong password."
    return render_template("base.html", player=current_player(), content_override=f"""
    <div class="card">
      <h2>Admin login</h2>
      {"<p class='err'>" + error + "</p>" if error else ""}
      <form method="post">
        <label>Password</label>
        <input type="password" name="password" autocomplete="current-password">
        <button class="btn" type="submit">Login</button>
      </form>
    </div>
    """)


@app.get("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("home"))


@app.route("/admin", methods=["GET", "POST"])
def admin():
    require_admin()

    msg = None
    err = None

    # Handle actions
    if request.method == "POST":
        action = request.form.get("action") or ""

        if action == "set_difficulty":
            if difficulty_locked():
                err = "Difficulty is locked after the first round is created."
            else:
                diff = request.form.get("difficulty") or "easy"
                if diff not in ("easy", "hard", "extreme"):
                    diff = "easy"
                set_setting("difficulty", diff)
                msg = f"Difficulty set to {diff}."

        elif action == "create_round":
            question = (request.form.get("question") or "").strip()
            # close any existing open round first
            with db_connect() as con:
                con.execute("UPDATE rounds SET status='closed', closed_at=? WHERE status='open'", (utc_now_iso(),))
                con.execute(
                    """
                    INSERT INTO rounds(question,status,created_at)
                    VALUES(?, 'open', ?)
                    """,
                    (question, utc_now_iso()),
                )
            msg = "Round created and opened."

        elif action == "set_answers":
            rid = request.form.get("round_id")
            if not rid:
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
                with db_connect() as con:
                    con.execute(
                        """
                        UPDATE rounds
                        SET correct_song=?, correct_artist=?, correct_year=?
                        WHERE id=? AND status='open'
                        """,
                        (cs, ca, cy, int(rid)),
                    )
                msg = "Correct answers updated."

        elif action == "close_round":
            rid = request.form.get("round_id")
            if not rid:
                err = "No open round."
            else:
                rid = int(rid)
                # Score + close
                compute_and_store_round_scores(rid)
                with db_connect() as con:
                    con.execute(
                        "UPDATE rounds SET status='closed', closed_at=? WHERE id=?",
                        (utc_now_iso(), rid),
                    )
                save_standings_snapshot(rid)
                msg = "Round closed and scored."

        elif action == "reset_game":
            # Keep players, wipe rounds/guesses/history, unlock difficulty
            with db_connect() as con:
                con.execute("DELETE FROM guesses")
                con.execute("DELETE FROM standings_history")
                con.execute("DELETE FROM rounds")
                con.execute("UPDATE settings SET value='easy' WHERE key='difficulty'")
            msg = "Game reset. Players kept; rounds cleared."

    # Page data
    open_round = get_open_round()
    diff = get_setting("difficulty", "easy")
    locked = difficulty_locked()

    with db_connect() as con:
        players = con.execute("SELECT id, name, email, created_at FROM players ORDER BY name COLLATE NOCASE").fetchall()

    played = []
    if open_round:
        with db_connect() as con:
            played = con.execute(
                """
                SELECT p.name AS player, g.updated_at
                FROM guesses g
                JOIN players p ON p.id=g.player_id
                WHERE g.round_id=?
                ORDER BY p.name COLLATE NOCASE
                """,
                (open_round["id"],),
            ).fetchall()

    # Minimal admin UI embedded (keeps your request focused on the 5 templates)
    return render_template(
        "base.html",
        player=current_player(),
        content_override=render_template(
            "_admin_inline.html",
            msg=msg,
            err=err,
            open_round=open_round,
            played=played,
            players=players,
            difficulty=diff,
            locked=locked,
        ),
    )


# Inline template used by admin route
# (so you don't have to manage another file right now)
@app.context_processor
def inject_admin_inline():
    def admin_inline_template():
        return """
{% if msg %}<p class="ok">{{ msg }}</p>{% endif %}
{% if err %}<p class="err">{{ err }}</p>{% endif %}

<div class="card">
  <h2>Admin</h2>

  <h3>Difficulty (choose before game start)</h3>
  <form method="post">
    <input type="hidden" name="action" value="set_difficulty">
    <select name="difficulty" {% if locked %}disabled{% endif %}>
      <option value="easy" {% if difficulty=='easy' %}selected{% endif %}>Easy</option>
      <option value="hard" {% if difficulty=='hard' %}selected{% endif %}>Hard</option>
      <option value="extreme" {% if difficulty=='extreme' %}selected{% endif %}>Extreme</option>
    </select>
    <button class="btn" type="submit" {% if locked %}disabled{% endif %}>Save</button>
    {% if locked %}<p class="muted">Locked after the first round is created.</p>{% endif %}
  </form>

  <hr>

  <h3>Create & open round</h3>
  <form method="post">
    <input type="hidden" name="action" value="create_round">
    <label>Question</label>
    <textarea name="question" rows="2" placeholder='e.g. "Song #3"'></textarea>
    <button class="btn" type="submit">Create + open</button>
  </form>
</div>

<div class="card">
  <h3>Open round</h3>
  {% if open_round %}
    <p><strong>#{{ open_round.id }}</strong> {{ open_round.question }}</p>

    <form method="post" style="margin-top:10px;">
      <input type="hidden" name="action" value="set_answers">
      <input type="hidden" name="round_id" value="{{ open_round.id }}">
      <label>Correct song</label>
      <input name="correct_song" value="{{ open_round.correct_song or '' }}">
      <label>Correct artist</label>
      <input name="correct_artist" value="{{ open_round.correct_artist or '' }}">
      <label>Correct year</label>
      <input name="correct_year" value="{{ open_round.correct_year or '' }}">
      <button class="btn" type="submit">Update correct answers</button>
    </form>

    <form method="post" style="margin-top:10px;">
      <input type="hidden" name="action" value="close_round">
      <input type="hidden" name="round_id" value="{{ open_round.id }}">
      <button class="btn secondary" type="submit">Close & score</button>
    </form>

    <h4 style="margin-top:14px;">Played this round</h4>
    {% if played and played|length > 0 %}
      <ul class="list">
        {% for p in played %}
          <li>{{ p.player }} <span class="muted">({{ p.updated_at }})</span></li>
        {% endfor %}
      </ul>
    {% else %}
      <p class="muted">No submissions yet.</p>
    {% endif %}
  {% else %}
    <p class="muted">No open round.</p>
  {% endif %}
</div>

<div class="card">
  <h3>Reset game</h3>
  <form method="post" onsubmit="return confirm('Reset rounds/guesses/history? Players kept.');">
    <input type="hidden" name="action" value="reset_game">
    <button class="btn danger" type="submit">Reset</button>
  </form>
</div>

<div class="card">
  <h3>Players</h3>
  {% if players and players|length > 0 %}
    <table>
      <thead><tr><th>Name</th><th>Email</th><th>Created</th></tr></thead>
      <tbody>
        {% for p in players %}
          <tr><td>{{ p.name }}</td><td>{{ p.email or '' }}</td><td>{{ p.created_at }}</td></tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p class="muted">No players yet.</p>
  {% endif %}
</div>
"""
    return {"_admin_inline_template": admin_inline_template}


@app.get("/_admin_inline.html")
def _admin_inline_html():
    # Internal-only: renderable snippet for admin page
    return render_template_string(app.context_processor_funcs[-1]()["_admin_inline_template"]())


# Flask needs this import for render_template_string above
from flask import render_template_string


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
