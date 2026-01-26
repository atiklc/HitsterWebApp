import sqlite3
from pathlib import Path
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, jsonify
)

# ========= SETTINGS =========
DB_PATH = Path("hitster.db")
ADMIN_PASSWORD = "0987654321"   # <<< CHANGE THIS in real use
# ============================


# ---------- DB helpers ----------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS players (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS rounds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question TEXT NOT NULL,
        correct_song TEXT,
        correct_artist TEXT,
        correct_year TEXT,
        status TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
        submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(player_id, round_id),
        FOREIGN KEY(player_id) REFERENCES players(id),
        FOREIGN KEY(round_id) REFERENCES rounds(id)
    )
    """)

    # New: snapshot of standings after each closed round
    cur.execute("""
    CREATE TABLE IF NOT EXISTS round_standings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER NOT NULL,
        player_id INTEGER NOT NULL,
        rank INTEGER NOT NULL,
        total_score INTEGER NOT NULL,
        FOREIGN KEY(round_id) REFERENCES rounds(id),
        FOREIGN KEY(player_id) REFERENCES players(id)
    )
    """)

    conn.commit()
    conn.close()


def get_current_player():
    player_id = session.get("player_id")
    if not player_id:
        return None
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM players WHERE id = ?", (player_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_current_open_round():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM rounds WHERE status = 'open' ORDER BY id DESC LIMIT 1"
    )
    row = cur.fetchone()
    conn.close()
    return row


def score_hitster_round(cur, round_row):
    """
    Hitster scoring:

      Song:   exact (case-insensitive) -> 5 pts
      Artist: exact                   -> 5 pts
      Year (absolute difference in years):

        diff = |guess - correct|

        0       -> 10 pts
        1       -> 8 pts
        2       -> 6 pts
        3–5     -> 4 pts
        6–10    -> 2 pts
        >10     -> -4 pts

      If year cannot be parsed as int, year score = 0.
    """
    round_id = round_row["id"]
    correct_song = (round_row["correct_song"] or "").strip().lower()
    correct_artist = (round_row["correct_artist"] or "").strip().lower()
    year_str = (round_row["correct_year"] or "").strip()

    try:
        correct_year = int(year_str)
    except ValueError:
        correct_year = None

    # All guesses for this round
    cur.execute("""
        SELECT id, answer_song, answer_artist, answer_year
        FROM guesses
        WHERE round_id = ?
    """, (round_id,))
    guesses = cur.fetchall()

    for g in guesses:
        gid = g["id"]
        song_guess = (g["answer_song"] or "").strip().lower()
        artist_guess = (g["answer_artist"] or "").strip().lower()
        year_guess_str = (g["answer_year"] or "").strip()

        # Song
        score_song = 5 if correct_song and song_guess == correct_song else 0

        # Artist
        score_artist = 5 if correct_artist and artist_guess == correct_artist else 0

        # Year
        score_year = 0
        if correct_year is not None and year_guess_str:
            try:
                guess_year = int(year_guess_str)
                diff = abs(guess_year - correct_year)

                if diff == 0:
                    score_year = 10
                elif diff == 1:
                    score_year = 8
                elif diff == 2:
                    score_year = 6
                elif 3 <= diff <= 5:
                    score_year = 4
                elif 6 <= diff <= 10:
                    score_year = 2
                else:  # diff > 10
                    score_year = -4
            except ValueError:
                score_year = 0

        total = score_song + score_artist + score_year

        cur.execute("""
            UPDATE guesses
            SET score_song = ?, score_artist = ?, score_year = ?, total_score = ?
            WHERE id = ?
        """, (score_song, score_artist, score_year, total, gid))


def snapshot_standings(cur, round_id):
    """
    Store a snapshot of standings after closing a round.
    Uses cumulative total_score across all guesses for each player.
    """
    # Remove any existing snapshot for this round (safety)
    cur.execute("DELETE FROM round_standings WHERE round_id = ?", (round_id,))

    # Compute current cumulative standings
    cur.execute("""
        SELECT
            p.id AS player_id,
            COALESCE(SUM(g.total_score), 0) AS total_score
        FROM players p
        LEFT JOIN guesses g ON p.id = g.player_id
        GROUP BY p.id
        ORDER BY total_score DESC, p.name ASC
    """)
    rows = cur.fetchall()

    rank = 1
    for r in rows:
        cur.execute("""
            INSERT INTO round_standings(round_id, player_id, rank, total_score)
            VALUES (?, ?, ?, ?)
        """, (round_id, r["player_id"], rank, r["total_score"]))
        rank += 1


def create_app():
    app = Flask(__name__)
    app.secret_key = "change-this-secret-too"  # <<< change this as well
    init_db()

    @app.route("/", methods=["GET", "POST"])
    def index():
        if request.method == "POST":
            name = request.form.get("player_name", "").strip()
            if not name:
                return render_template("index.html", error="Please enter a name.")

            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM players WHERE name = ?", (name,))
            row = cur.fetchone()
            if row:
                player_id = row["id"]
            else:
                cur.execute("INSERT INTO players(name) VALUES (?)", (name,))
                conn.commit()
                player_id = cur.lastrowid
            conn.close()

            session["player_id"] = player_id
            return redirect(url_for("game"))

        player = get_current_player()
        return render_template("index.html", player=player)

    @app.route("/game", methods=["GET", "POST"])
    def game():
        player = get_current_player()
        if not player:
            return redirect(url_for("index"))

        round_row = get_current_open_round()
        if not round_row:
            return render_template(
                "game.html",
                player=player,
                current_round=None,
                already_answered=False
            )

        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM guesses WHERE player_id = ? AND round_id = ?",
            (player["id"], round_row["id"])
        )
        guess = cur.fetchone()
        conn.close()

        already_answered = guess is not None

        if request.method == "POST":
            if already_answered:
                return redirect(url_for("game"))

            song = (request.form.get("answer_song", "") or "").strip()
            artist = (request.form.get("answer_artist", "") or "").strip()
            year = (request.form.get("answer_year", "") or "").strip()

            if not song and not artist and not year:
                return render_template(
                    "game.html",
                    player=player,
                    current_round=round_row,
                    already_answered=already_answered,
                    error="Please enter at least one field."
                )

            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO guesses(player_id, round_id, answer_song, answer_artist, answer_year)
                VALUES (?,?,?,?,?)
            """, (player["id"], round_row["id"], song, artist, year))
            conn.commit()
            conn.close()
            return redirect(url_for("game"))

        return render_template(
            "game.html",
            player=player,
            current_round=round_row,
            already_answered=already_answered
        )

    @app.route("/standings")
    def standings():
        return render_template("standings.html")

    @app.route("/api/standings")
    def api_standings():
        """
        Returns JSON:
        [
          {"player": "...", "score": 42, "rank": 1, "delta": +1},
          ...
        ]

        - rank  = current rank after last closed round
        - delta = (previous_rank - current_rank)
                  >0  => moved UP (better)
                  <0  => moved DOWN
                  0   => same / new
        """
        conn = get_db()
        cur = conn.cursor()

        # Try to use snapshots from last closed round(s)
        cur.execute("""
            SELECT DISTINCT round_id
            FROM round_standings
            ORDER BY round_id DESC
            LIMIT 2
        """)
        round_rows = cur.fetchall()

        data = []

        if round_rows:
            latest_round_id = round_rows[0]["round_id"]
            prev_round_id = round_rows[1]["round_id"] if len(round_rows) > 1 else None

            # Current standings from latest snapshot
            cur.execute("""
                SELECT rs.player_id, rs.rank, rs.total_score, p.name
                FROM round_standings rs
                JOIN players p ON p.id = rs.player_id
                WHERE rs.round_id = ?
                ORDER BY rs.rank ASC
            """, (latest_round_id,))
            current_rows = cur.fetchall()

            prev_ranks = {}
            if prev_round_id is not None:
                cur.execute("""
                    SELECT player_id, rank
                    FROM round_standings
                    WHERE round_id = ?
                """, (prev_round_id,))
                for r in cur.fetchall():
                    prev_ranks[r["player_id"]] = r["rank"]

            for r in current_rows:
                pid = r["player_id"]
                current_rank = r["rank"]
                prev_rank = prev_ranks.get(pid)
                if prev_rank is None:
                    delta = 0  # treat as no change / new
                else:
                    # Positive delta means moved UP (towards rank 1)
                    delta = prev_rank - current_rank

                data.append({
                    "player": r["name"],
                    "score": r["total_score"],
                    "rank": current_rank,
                    "delta": delta
                })

        else:
            # No snapshots yet: fall back to live cumulative standings
            cur.execute("""
                SELECT p.name, COALESCE(SUM(g.total_score), 0) AS total_score
                FROM players p
                LEFT JOIN guesses g ON p.id = g.player_id
                GROUP BY p.id
                ORDER BY total_score DESC, p.name ASC
            """)
            rows = cur.fetchall()
            rank = 1
            for r in rows:
                data.append({
                    "player": r["name"],
                    "score": r["total_score"],
                    "rank": rank,
                    "delta": 0
                })
                rank += 1

        conn.close()
        return jsonify(data)

    @app.route("/admin", methods=["GET", "POST"])
    def admin():
        is_admin = session.get("is_admin", False)
        error = None

        if not is_admin:
            if request.method == "POST" and request.form.get("action") == "login":
                pw = request.form.get("password", "")
                if pw == ADMIN_PASSWORD:
                    session["is_admin"] = True
                    return redirect(url_for("admin"))
                else:
                    error = "Wrong password."
            return render_template(
                "admin.html",
                is_admin=False,
                error=error,
                current_round=None,
                rounds=[],
                current_round_players=[]
            )

        conn = get_db()
        cur = conn.cursor()

        if request.method == "POST":
            action = request.form.get("action")

            if action == "logout_admin":
                session.pop("is_admin", None)
                conn.close()
                return redirect(url_for("admin"))

            elif action == "create_round":
                question = request.form.get("question", "").strip()
                correct_song = request.form.get("correct_song", "").strip()
                correct_artist = request.form.get("correct_artist", "").strip()
                correct_year = request.form.get("correct_year", "").strip()
                if question:
                    cur.execute("""
                        INSERT INTO rounds(question, correct_song, correct_artist, correct_year, status)
                        VALUES (?,?,?,?, 'open')
                    """, (question, correct_song, correct_artist, correct_year))
                    conn.commit()

            elif action == "set_answers":
                correct_song = request.form.get("correct_song", "").strip()
                correct_artist = request.form.get("correct_artist", "").strip()
                correct_year = request.form.get("correct_year", "").strip()
                cur.execute(
                    "SELECT id FROM rounds WHERE status = 'open' ORDER BY id DESC LIMIT 1"
                )
                r = cur.fetchone()
                if r:
                    cur.execute("""
                        UPDATE rounds
                        SET correct_song = ?, correct_artist = ?, correct_year = ?
                        WHERE id = ?
                    """, (correct_song or None, correct_artist or None, correct_year or None, r["id"]))
                    conn.commit()

            elif action == "close_round":
                cur.execute(
                    "SELECT * FROM rounds WHERE status = 'open' ORDER BY id DESC LIMIT 1"
                )
                round_row = cur.fetchone()
                if round_row:
                    # 1) score the round
                    score_hitster_round(cur, round_row)
                    # 2) mark closed
                    cur.execute(
                        "UPDATE rounds SET status = 'closed' WHERE id = ?",
                        (round_row["id"],)
                    )
                    # 3) snapshot standings
                    snapshot_standings(cur, round_row["id"])
                    conn.commit()

            elif action == "reset_game":
                # Delete all rounds, guesses, and snapshots; keep players
                cur.execute("DELETE FROM round_standings")
                cur.execute("DELETE FROM guesses")
                cur.execute("DELETE FROM rounds")
                conn.commit()

        # Fetch current open round
        cur.execute(
            "SELECT * FROM rounds WHERE status = 'open' ORDER BY id DESC LIMIT 1"
        )
        current_round = cur.fetchone()

        # Fetch recent rounds
        cur.execute("SELECT * FROM rounds ORDER BY id DESC LIMIT 20")
        rounds = cur.fetchall()

        # Fetch players + whether they answered this round
        current_round_players = []
        if current_round:
            cur.execute("""
                SELECT
                    p.name,
                    g.answer_song,
                    g.answer_artist,
                    g.answer_year
                FROM players p
                LEFT JOIN guesses g
                  ON p.id = g.player_id AND g.round_id = ?
                ORDER BY p.name
            """, (current_round["id"],))
            current_round_players = cur.fetchall()

        conn.close()

        return render_template(
            "admin.html",
            is_admin=True,
            error=error,
            current_round=current_round,
            rounds=rounds,
            current_round_players=current_round_players
        )

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
