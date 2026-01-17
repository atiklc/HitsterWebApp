import sqlite3
from pathlib import Path
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, jsonify
)

# Use a fresh DB name to avoid old schema conflicts
DB_PATH = Path("hitster.db")


# ---------- DB & helpers ----------

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
        status TEXT NOT NULL,         -- 'open' or 'closed'
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
    Score all guesses for a round using Hitster rules:
    - Song: exact (case-insensitive) -> 5 pts
    - Artist: exact -> 5 pts
    - Year:
        exact: 5
        1 year off: 4
        2 years off: 3
        if ALL valid guesses are >=3 away:
          closest diff -> 1
          others -> 0
    """
    round_id = round_row["id"]
    correct_song = (round_row["correct_song"] or "").strip().lower()
    correct_artist = (round_row["correct_artist"] or "").strip().lower()
    year_str = (round_row["correct_year"] or "").strip()

    try:
        correct_year = int(year_str)
    except ValueError:
        correct_year = None

    # Fetch all guesses for this round
    cur.execute("""
        SELECT id, answer_song, answer_artist, answer_year
        FROM guesses
        WHERE round_id = ?
    """, (round_id,))
    guesses = cur.fetchall()

    # Compute year diffs for valid guesses
    diff_by_id = {}
    if correct_year is not None:
        for g in guesses:
            ys = (g["answer_year"] or "").strip()
            try:
                y = int(ys)
                diff_by_id[g["id"]] = abs(y - correct_year)
            except ValueError:
                diff_by_id[g["id"]] = None
    else:
        for g in guesses:
            diff_by_id[g["id"]] = None

    valid_diffs = [d for d in diff_by_id.values() if d is not None]
    has_close = False
    min_diff = None

    if correct_year is not None and valid_diffs:
        has_close = any(d <= 2 for d in valid_diffs)
        if not has_close:
            min_diff = min(valid_diffs)

    # Score each guess
    for g in guesses:
        gid = g["id"]
        song_guess = (g["answer_song"] or "").strip().lower()
        artist_guess = (g["answer_artist"] or "").strip().lower()
        y_diff = diff_by_id.get(gid)

        # Song
        score_song = 5 if correct_song and song_guess == correct_song else 0
        # Artist
        score_artist = 5 if correct_artist and artist_guess == correct_artist else 0

        # Year
        score_year = 0
        if correct_year is not None and y_diff is not None:
            if has_close:
                if y_diff == 0:
                    score_year = 5
                elif y_diff == 1:
                    score_year = 4
                elif y_diff == 2:
                    score_year = 3
            else:
                # all diffs >= 3; closest gets 1
                if min_diff is not None and y_diff == min_diff:
                    score_year = 1

        total = score_song + score_artist + score_year

        cur.execute("""
            UPDATE guesses
            SET score_song = ?, score_artist = ?, score_year = ?, total_score = ?
            WHERE id = ?
        """, (score_song, score_artist, score_year, total, gid))


# ---------- App factory & routes ----------

def create_app():
    app = Flask(__name__)
    app.secret_key = "change-this-to-a-random-secret"

    init_db()

    @app.route("/", methods=["GET", "POST"])
    def index():
        # Join / choose player name
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
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT p.name, COALESCE(SUM(g.total_score), 0) AS total_score
            FROM players p
            LEFT JOIN guesses g ON p.id = g.player_id
            GROUP BY p.id
            ORDER BY total_score DESC, p.name ASC
        """)
        rows = cur.fetchall()
        conn.close()
        data = [
            {"player": r["name"], "score": r["total_score"]}
            for r in rows
        ]
        return jsonify(data)

    @app.route("/admin", methods=["GET", "POST"])
    def admin():
        conn = get_db()
        cur = conn.cursor()

        if request.method == "POST":
            action = request.form.get("action")

            if action == "create_round":
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
                    score_hitster_round(cur, round_row)
                    cur.execute(
                        "UPDATE rounds SET status = 'closed' WHERE id = ?",
                        (round_row["id"],)
                    )
                    conn.commit()

        cur.execute("SELECT * FROM rounds ORDER BY id DESC LIMIT 20")
        rounds = cur.fetchall()
        conn.close()

        current_round = get_current_open_round()
        return render_template(
            "admin.html",
            current_round=current_round,
            rounds=rounds
        )

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
