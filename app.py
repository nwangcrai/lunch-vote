import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_autorefresh import st_autorefresh

UP_COLOR = "#2a78d6"
DOWN_COLOR = "#e34948"
MUTED_COLOR = "#898781"

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
    # "CREATE TABLE IF NOT EXISTS" is a no-op against a votes.db left over from an older
    # schema version, so drop and recreate if the table predates the sentiment column.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(votes)")}
    if existing_cols and "sentiment" not in existing_cols:
        conn.execute("DROP TABLE votes")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY,
            voter_name TEXT NOT NULL,
            restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
            vote_date TEXT NOT NULL,
            sentiment TEXT NOT NULL CHECK(sentiment IN ('up', 'down')),
            UNIQUE(voter_name, restaurant_id, vote_date)
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


def cast_vote(voter_name: str, restaurant_name: str, sentiment: str) -> None:
    """Upsert a thumbs up/down vote. Voting the same sentiment again clears it (toggle off)."""
    conn = get_connection()
    restaurant_id = conn.execute(
        "SELECT id FROM restaurants WHERE name = ?", (restaurant_name,)
    ).fetchone()[0]
    today = date.today().isoformat()

    existing = conn.execute(
        "SELECT sentiment FROM votes WHERE voter_name = ? AND restaurant_id = ? AND vote_date = ?",
        (voter_name, restaurant_id, today),
    ).fetchone()

    if existing and existing[0] == sentiment:
        conn.execute(
            "DELETE FROM votes WHERE voter_name = ? AND restaurant_id = ? AND vote_date = ?",
            (voter_name, restaurant_id, today),
        )
    else:
        conn.execute(
            """
            INSERT INTO votes (voter_name, restaurant_id, vote_date, sentiment)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(voter_name, restaurant_id, vote_date)
            DO UPDATE SET sentiment = excluded.sentiment
            """,
            (voter_name, restaurant_id, today, sentiment),
        )
    conn.commit()
    conn.close()


def get_my_votes(voter_name: str) -> dict[str, str]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT r.name, v.sentiment FROM votes v
        JOIN restaurants r ON r.id = v.restaurant_id
        WHERE v.voter_name = ? AND v.vote_date = ?
        """,
        (voter_name, date.today().isoformat()),
    ).fetchall()
    conn.close()
    return dict(rows)


def get_today_results() -> pd.DataFrame:
    conn = get_connection()
    df = pd.read_sql_query(
        """
        SELECT
            r.name AS restaurant,
            SUM(CASE WHEN v.sentiment = 'up' THEN 1 ELSE 0 END) AS thumbs_up,
            SUM(CASE WHEN v.sentiment = 'down' THEN 1 ELSE 0 END) AS thumbs_down
        FROM restaurants r
        LEFT JOIN votes v ON v.restaurant_id = r.id AND v.vote_date = ?
        GROUP BY r.name
        """,
        conn,
        params=(date.today().isoformat(),),
    )
    conn.close()
    df["net_score"] = df["thumbs_up"] - df["thumbs_down"]
    df["total_votes"] = df["thumbs_up"] + df["thumbs_down"]
    return df.sort_values(
        ["net_score", "total_votes"], ascending=False
    ).reset_index(drop=True)


def build_tug_of_war_chart(results: pd.DataFrame) -> go.Figure:
    """One horizontal bar per restaurant, diverging from a center zero-line:
    red (thumbs down) extends left, blue (thumbs up) extends right, lengths are
    raw vote counts. Counts are labeled just outside each bar's tip."""
    df = results.sort_values("net_score", ascending=True).reset_index(drop=True)
    restaurants = df["restaurant"].tolist()
    n = len(df)

    down_x = (-df["thumbs_down"]).tolist()
    up_x = df["thumbs_up"].tolist()
    max_count = max(df["thumbs_up"].max(), df["thumbs_down"].max(), 1)
    pad = max_count * 0.25

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            name="👎 Down",
            x=down_x,
            y=restaurants,
            orientation="h",
            marker_color=DOWN_COLOR,
            text=[str(v) if v else "" for v in df["thumbs_down"]],
            textposition="outside",
            hovertemplate="%{y}<br>👎 %{customdata} down<extra></extra>",
            customdata=df["thumbs_down"],
        )
    )
    fig.add_trace(
        go.Bar(
            name="👍 Up",
            x=up_x,
            y=restaurants,
            orientation="h",
            marker_color=UP_COLOR,
            text=[str(v) if v else "" for v in df["thumbs_up"]],
            textposition="outside",
            hovertemplate="%{y}<br>👍 %{customdata} up<extra></extra>",
            customdata=df["thumbs_up"],
        )
    )

    # Center reference line = a tied 0-0 pull.
    fig.add_vline(x=0, line_width=1, line_color=MUTED_COLOR, line_dash="dot")

    fig.update_layout(
        barmode="overlay",
        bargap=0.35,
        xaxis=dict(
            range=[-max_count - pad, max_count + pad],
            showticklabels=False,
            showgrid=False,
            zeroline=False,
        ),
        yaxis=dict(title=None, automargin=True),
        height=70 + 46 * n,
        margin=dict(l=10, r=10, t=10, b=10, autoexpand=True),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        paper_bgcolor="#fcfcfb",
        plot_bgcolor="#fcfcfb",
    )
    return fig


init_db()

st.set_page_config(page_title="Lunch Vote", page_icon="🍽️")
st_autorefresh(interval=7_000, key="lunch_vote_refresh")

st.title("Where's lunch today? 🍽️")
st.caption(date.today().strftime("%A, %B %d, %Y"))

voter_name = st.text_input("Your name", value=st.session_state.get("voter_name", ""))
st.session_state["voter_name"] = voter_name

restaurants = get_restaurants()

st.subheader("Vote for each restaurant")
if voter_name:
    my_votes = get_my_votes(voter_name)
    for name in restaurants:
        col_name, col_up, col_down = st.columns([4, 1, 1])
        current = my_votes.get(name)
        col_name.write(name)

        up_type = "primary" if current == "up" else "secondary"
        down_type = "primary" if current == "down" else "secondary"

        if col_up.button("👍", key=f"up_{name}", type=up_type):
            cast_vote(voter_name, name, "up")
            st.rerun()
        if col_down.button("👎", key=f"down_{name}", type=down_type):
            cast_vote(voter_name, name, "down")
            st.rerun()
    st.caption("Click a thumb again to remove your vote.")
else:
    st.info("Enter your name to vote.")

st.subheader("Live results")
results = get_today_results()
total_votes = int(results["total_votes"].sum())
st.caption(f"{total_votes} vote(s) so far today · dotted line marks a tie (0-0)")

st.plotly_chart(build_tug_of_war_chart(results), use_container_width=True)

with st.expander("Exact counts"):
    st.dataframe(
        results.rename(
            columns={
                "thumbs_up": "👍",
                "thumbs_down": "👎",
                "net_score": "Net Score",
                "total_votes": "Total",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )
