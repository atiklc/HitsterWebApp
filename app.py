import sqlite3
from pathlib import Path
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, jsonify
)

DB_PATH = Path("game.db")


def create_app():
    app = Flask(__name__)
    app.secret_key = "change-this-to-a-random-secret"

    # --- DB helpers ---

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
            correct_answer TEXT,
            status TEXT NOT NULL,         -- 'open' or 'closed'
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS guesses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            round_id INTEGER NOT NULL,
            answer TEXT NOT NULL,
            score INTEGER DEFAULT 0,
            submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(player_id, round_id),
            FOREIGN KEY(player_id) REFERENCES players(id),
            FOREIGN KEY(round_id) REFERENCES rounds(id)
        )
        """)
        conn.commit()
        conn.close()

    init_db()

    # --- Helpers ---

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

    # --- Routes ---

    @app.route("/", methods=["GET", "POST"])
    def index():
        # Player registration / selection
        if request.method == "POST":
            name = request.form.get("player_name", "").strip()
            if not name:
                return render_template("index.html", error="Please enter a name.")

            conn = get_db()
            cur = conn.cursor()
            # Create player if not exists
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
            return redirect(url_for("play"))

        player = get_current_player()
        return render_template("index.html", player=player)

    @app.route("/play", methods=["GET", "POST"])
    def play():
        player = get_current_player()
        if not player:
            return redirect(url_for("index"))

        round_row = get_current_open_round()
        if not round_row:
            # No open round yet
            return render_template("play.html", player=player, current_round=None, already_answered=False)

        conn = get_db()
        cur = conn.cursor()
        # Check if player already answered this round
        cur.execute(
            "SELECT * FROM guesses WHERE player_id = ? AND round_id = ?",
            (player["id"], round_row["id"])
        )
        guess = cur.fetchone()
        conn.close()

        already_answered = guess is not None

        if request.method == "POST":
            if already_answered:
                # For simplicity, ignore extra submissions
                return redirect(url_for("play"))

            answer = request.form.get("answer", "").strip()
            if not answer:
                return render_template(
                    "play.html",
                    player=player,
                    current_round=round_row,
                    already_answered=already_answered,
                    error="Please enter an answer."
                )

            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "INSERT OR REPLACE INTO guesses(player_id, round_id, answer) VALUES (?,?,?)",
                (player["id"], round_row["id"], answer)
            )
            conn.commit()
            conn.close()
            return redirect(url_for("play"))

        return render_template(
            "play.html",
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
            SELECT p.name, COALESCE(SUM(g.score), 0) AS total_score
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
                correct_answer = request.form.get("correct_answer", "").strip()
                if question:
                    cur.execute(
                        "INSERT INTO rounds(question, correct_answer, status) VALUES (?,?, 'open')",
                        (question, correct_answer)
                    )
                    conn.commit()

            elif action == "close_round":
                # Close currently open round and score it
                cur.execute(
                    "SELECT * FROM rounds WHERE status = 'open' ORDER BY id DESC LIMIT 1"
                )
                round_row = cur.fetchone()
                if round_row:
                    round_id = round_row["id"]
                    correct = (round_row["correct_answer"] or "").strip().lower()

                    # Score guesses
                    cur.execute(
                        "SELECT id, answer FROM guesses WHERE round_id = ?",
                        (round_id,)
                    )
                    guesses = cur.fetchall()
                    for g in guesses:
                        ans = (g["answer"] or "").strip().lower()
                        score = 1 if correct and ans == correct else 0
                        cur.execute(
                            "UPDATE guesses SET score = ? WHERE id = ?",
                            (score, g["id"])
                        )

                    # Close the round
                    cur.execute(
                        "UPDATE rounds SET status = 'closed' WHERE id = ?",
                        (round_id,)
                    )
                    conn.commit()

            elif action == "open_next_round":
                # Just open a new round with empty correct answer
                question = request.form.get("question", "").strip()
                if question:
                    cur.execute(
                        "INSERT INTO rounds(question, status) VALUES (?, 'open')",
                        (question,)
                    )
                    conn.commit()

        # fetch rounds list
        cur.execute(
            "SELECT * FROM rounds ORDER BY id DESC LIMIT 20"
        )
        rounds = cur.fetchall()
        conn.close()

        current_round = get_current_open_round()
        return render_template(
            "admin.html",
            current_round=current_round,
            rounds=rounds
        )

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, host="0.0.0.0", port=5000)
