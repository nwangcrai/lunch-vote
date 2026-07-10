import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

DB_PATH = Path(__file__).parent / "votes.db"
RESTAURANTS_CSV = Path(__file__).parent / "restaurants.csv"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS restaurants (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY,
            voter_name TEXT NOT NULL,
            restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
            vote_date TEXT NOT NULL,
            UNIQUE(voter_name, vote_date)
        )
        """
    )

    names = pd.read_csv(RESTAURANTS_CSV)["name"].tolist()
    conn.executemany(
        "INSERT OR IGNORE INTO restaurants (name) VALUES (?)",
        [(n,) for n in names],
    )
    conn.commit()
    conn.close()


def get_restaurants() -> list[str]:
    conn = get_connection()
    names = [row[0] for row in conn.execute("SELECT name FROM restaurants ORDER BY name")]
    conn.close()
    return names


def cast_vote(voter_name: str, restaurant_name: str) -> None:
    conn = get_connection()
    restaurant_id = conn.execute(
        "SELECT id FROM restaurants WHERE name = ?", (restaurant_name,)
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO votes (voter_name, restaurant_id, vote_date)
        VALUES (?, ?, ?)
        ON CONFLICT(voter_name, vote_date)
        DO UPDATE SET restaurant_id = excluded.restaurant_id
        """,
        (voter_name, restaurant_id, date.today().isoformat()),
    )
    conn.commit()
    conn.close()


def get_my_vote(voter_name: str) -> str | None:
    conn = get_connection()
    row = conn.execute(
        """
        SELECT r.name FROM votes v
        JOIN restaurants r ON r.id = v.restaurant_id
        WHERE v.voter_name = ? AND v.vote_date = ?
        """,
        (voter_name, date.today().isoformat()),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def get_today_results() -> pd.DataFrame:
    conn = get_connection()
    df = pd.read_sql_query(
        """
        SELECT r.name AS restaurant, COUNT(v.id) AS votes
        FROM restaurants r
        LEFT JOIN votes v ON v.restaurant_id = r.id AND v.vote_date = ?
        GROUP BY r.name
        ORDER BY votes DESC, restaurant ASC
        """,
        conn,
        params=(date.today().isoformat(),),
    )
    conn.close()
    return df


init_db()

st.set_page_config(page_title="Lunch Vote", page_icon="🍽️")
st_autorefresh(interval=7_000, key="lunch_vote_refresh")

st.title("Where's lunch today? 🍽️")
st.caption(date.today().strftime("%A, %B %d, %Y"))

voter_name = st.text_input("Your name", value=st.session_state.get("voter_name", ""))
st.session_state["voter_name"] = voter_name

restaurants = get_restaurants()

if voter_name:
    current_vote = get_my_vote(voter_name)
    default_index = restaurants.index(current_vote) if current_vote in restaurants else 0
    choice = st.radio("Pick a restaurant", restaurants, index=default_index)

    if st.button("Vote"):
        cast_vote(voter_name, choice)
        st.success(f"Vote recorded for {choice}!")
        st.rerun()
else:
    st.info("Enter your name to vote.")

st.subheader("Live results")
results = get_today_results()
total_votes = int(results["votes"].sum())
st.caption(f"{total_votes} vote(s) so far today")

if total_votes > 0:
    st.bar_chart(results.set_index("restaurant")["votes"])
else:
    st.write("No votes yet — be the first!")

st.dataframe(results, use_container_width=True, hide_index=True)
