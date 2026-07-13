import base64
import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

UP_COLOR = "#008300"
UP_COLOR_HOVER = "#006b00"
DOWN_COLOR = "#e34948"
MUTED_COLOR = "#898781"

DB_PATH = Path(__file__).parent / "votes.db"
RESTAURANTS_CSV = Path(__file__).parent / "restaurants.csv"
THUMBS_UP_PNG = Path(__file__).parent / "thumbs_up.png"
THUMBS_DOWN_PNG = Path(__file__).parent / "thumbs_down.png"


def image_data_uri(path: Path) -> str:
    return f"data:image/png;base64,{base64.b64encode(path.read_bytes()).decode()}"


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
            name TEXT UNIQUE NOT NULL,
            description TEXT NOT NULL DEFAULT ''
        )
        """
    )
    # "CREATE TABLE IF NOT EXISTS" won't add a column to a restaurants table left over
    # from before descriptions existed.
    if "description" not in {row[1] for row in conn.execute("PRAGMA table_info(restaurants)")}:
        conn.execute("ALTER TABLE restaurants ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    # "CREATE TABLE IF NOT EXISTS" is a no-op against a votes.db left over from an older
    # schema version, so drop and recreate if the table predates the sentiment column or
    # still carries the retired vote_date column (votes are now permanent until a manual
    # reset, not daily-scoped).
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(votes)")}
    if existing_cols and ("sentiment" not in existing_cols or "vote_date" in existing_cols):
        conn.execute("DROP TABLE votes")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY,
            voter_name TEXT NOT NULL,
            restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
            sentiment TEXT NOT NULL CHECK(sentiment IN ('up', 'down')),
            UNIQUE(voter_name, restaurant_id)
        )
        """
    )

    restaurants_df = pd.read_csv(RESTAURANTS_CSV)
    conn.executemany(
        """
        INSERT INTO restaurants (name, description) VALUES (?, ?)
        ON CONFLICT(name) DO UPDATE SET description = excluded.description
        """,
        list(restaurants_df[["name", "description"]].itertuples(index=False, name=None)),
    )
    conn.commit()
    conn.close()


def cast_vote(voter_name: str, restaurant_name: str, sentiment: str) -> None:
    """Upsert a thumbs up/down vote. Voting the same sentiment again clears it (toggle off).
    Votes are permanent (one per person per restaurant) until an admin reset clears the table."""
    conn = get_connection()
    restaurant_id = conn.execute(
        "SELECT id FROM restaurants WHERE name = ?", (restaurant_name,)
    ).fetchone()[0]

    existing = conn.execute(
        "SELECT sentiment FROM votes WHERE voter_name = ? AND restaurant_id = ?",
        (voter_name, restaurant_id),
    ).fetchone()

    if existing and existing[0] == sentiment:
        conn.execute(
            "DELETE FROM votes WHERE voter_name = ? AND restaurant_id = ?",
            (voter_name, restaurant_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO votes (voter_name, restaurant_id, sentiment)
            VALUES (?, ?, ?)
            ON CONFLICT(voter_name, restaurant_id)
            DO UPDATE SET sentiment = excluded.sentiment
            """,
            (voter_name, restaurant_id, sentiment),
        )
    conn.commit()
    conn.close()


def reset_all_votes() -> None:
    conn = get_connection()
    conn.execute("DELETE FROM votes")
    conn.commit()
    conn.close()


def get_my_votes(voter_name: str) -> dict[str, str]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT r.name, v.sentiment FROM votes v
        JOIN restaurants r ON r.id = v.restaurant_id
        WHERE v.voter_name = ?
        """,
        (voter_name,),
    ).fetchall()
    conn.close()
    return dict(rows)


def get_respondent_count() -> int:
    conn = get_connection()
    count = conn.execute("SELECT COUNT(DISTINCT voter_name) FROM votes").fetchone()[0]
    conn.close()
    return count


def get_results() -> pd.DataFrame:
    conn = get_connection()
    df = pd.read_sql_query(
        """
        SELECT
            r.name AS restaurant,
            r.description AS description,
            SUM(CASE WHEN v.sentiment = 'up' THEN 1 ELSE 0 END) AS thumbs_up,
            SUM(CASE WHEN v.sentiment = 'down' THEN 1 ELSE 0 END) AS thumbs_down
        FROM restaurants r
        LEFT JOIN votes v ON v.restaurant_id = r.id
        GROUP BY r.name, r.description
        """,
        conn,
    )
    conn.close()
    df["net_score"] = df["thumbs_up"] - df["thumbs_down"]
    df["total_votes"] = df["thumbs_up"] + df["thumbs_down"]
    return df.sort_values(
        ["net_score", "total_votes"], ascending=False
    ).reset_index(drop=True)


def render_diverging_bar(down_count: int, up_count: int, max_count: int) -> None:
    """A center-anchored HTML bar: red (down) grows left, green (up) grows right,
    scaled against max_count so every restaurant's row shares one axis."""
    down_pct = (down_count / max_count * 50) if max_count else 0
    up_pct = (up_count / max_count * 50) if max_count else 0
    down_label = str(down_count) if down_count else ""
    up_label = str(up_count) if up_count else ""

    st.markdown(
        f"""
        <div style="display:flex; align-items:center; height:38px;">
          <div style="flex:1; display:flex; justify-content:flex-end; align-items:center;">
            <span style="margin-right:6px; color:{MUTED_COLOR}; font-size:13px;">{down_label}</span>
            <div style="width:{down_pct}%; height:14px; background:{DOWN_COLOR}; border-radius:3px 0 0 3px;"></div>
          </div>
          <div style="width:2px; height:22px; background:{MUTED_COLOR}; flex-shrink:0;"></div>
          <div style="flex:1; display:flex; justify-content:flex-start; align-items:center;">
            <div style="width:{up_pct}%; height:14px; background:{UP_COLOR}; border-radius:0 3px 3px 0;"></div>
            <span style="margin-left:6px; color:{MUTED_COLOR}; font-size:13px;">{up_label}</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


init_db()

st.set_page_config(page_title="Lunch Vote", page_icon="🍽️", layout="wide")
st_autorefresh(interval=7_000, key="lunch_vote_refresh")

# Use more of the page width instead of Streamlit's default centered, margin-heavy layout.
st.markdown(
    """
    <style>
    .block-container {
        max-width: 100%;
        padding-left: 2rem;
        padding-right: 2rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Vote buttons show the thumbs_up.png/thumbs_down.png icons instead of their text label
# (emptied to a non-breaking space, since st.button requires a non-empty label).
# Streamlit's default "primary" button color is red, which already matches the down-vote
# button. Only the up-vote button (key prefix "up_") needs to be overridden to green.
st.markdown(
    f"""
    <style>
    [class*="st-key-up_"] button, [class*="st-key-down_"] button {{
        color: transparent;
        background-repeat: no-repeat;
        background-position: center;
        background-size: 20px 20px;
    }}
    [class*="st-key-up_"] button {{
        background-image: url({image_data_uri(THUMBS_UP_PNG)});
    }}
    [class*="st-key-down_"] button {{
        background-image: url({image_data_uri(THUMBS_DOWN_PNG)});
    }}
    [class*="st-key-up_"] button[data-testid="stBaseButton-primary"] {{
        background-color: {UP_COLOR};
        border-color: {UP_COLOR};
    }}
    [class*="st-key-up_"] button[data-testid="stBaseButton-primary"]:hover {{
        background-color: {UP_COLOR_HOVER};
        border-color: {UP_COLOR_HOVER};
    }}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("DC Office Lunch Votes")
st.caption(date.today().strftime("%A, %B %d, %Y"))

st.text_input("Your name", key="voter_name")
voter_name = st.session_state["voter_name"]

if not voter_name:
    st.info(
        "Enter your name above to vote. Duplicates will be ignored, so please use your real name.",
        icon=None,
    )
    st.markdown(
        """
        <style>
        [data-testid="stAlertContainer"] p { font-size: 14px; }
        </style>
        """,
        unsafe_allow_html=True,
    )

st.subheader("Your vote & Live total results")
results = get_results()
respondent_count = get_respondent_count()
st.caption(f"{respondent_count} respondent(s) so far. Bars share one scale")

max_count = max(int(results["thumbs_up"].max()), int(results["thumbs_down"].max()), 1)
my_votes = get_my_votes(voter_name) if voter_name else {}

COLUMN_RATIOS = [3, 3, 2, 4, 1]


def centered(text: str, nowrap: bool = False) -> str:
    style = "text-align:center;"
    if nowrap:
        style += " white-space:nowrap;"
    return f"<div style='{style}'>{text}</div>"


header_name, header_desc, header_vote, header_bar, header_net = st.columns(COLUMN_RATIOS)
header_name.markdown("<div style='white-space:nowrap;'>Restaurant</div>", unsafe_allow_html=True)
header_desc.markdown("Description")
header_vote.markdown(centered("Your vote"), unsafe_allow_html=True)
header_bar.markdown(centered("Votes"), unsafe_allow_html=True)
header_net.markdown(centered("Net Score", nowrap=True), unsafe_allow_html=True)
st.markdown(
    f"<hr style='margin:2px 0 8px 0; border:none; border-top:1px solid {MUTED_COLOR};'>",
    unsafe_allow_html=True,
)

for _, row in results.iterrows():
    name = row["restaurant"]
    description = row["description"]
    down_count = int(row["thumbs_down"])
    up_count = int(row["thumbs_up"])
    net_score = int(row["net_score"])
    current = my_votes.get(name)

    col_name, col_desc, col_vote, col_bar, col_net = st.columns(COLUMN_RATIOS)
    col_name.markdown(f"<div style='white-space:nowrap;'><strong>{name}</strong></div>", unsafe_allow_html=True)
    col_desc.markdown(description)
    with col_bar:
        render_diverging_bar(down_count, up_count, max_count)
    net_label = f"{net_score:+d}" if net_score else "0"
    col_net.markdown(centered(net_label, nowrap=True), unsafe_allow_html=True)

    down_type = "primary" if current == "down" else "secondary"
    up_type = "primary" if current == "up" else "secondary"

    col_down, col_up = col_vote.columns(2)
    if col_down.button(" ", key=f"down_{name}", type=down_type, disabled=not voter_name):
        cast_vote(voter_name, name, "down")
        st.rerun()
    if col_up.button(" ", key=f"up_{name}", type=up_type, disabled=not voter_name):
        cast_vote(voter_name, name, "up")
        st.rerun()

if voter_name:
    st.caption("Click a thumb again to remove your vote.")

with st.expander("Admin: reset votes"):
    reset_password = st.text_input("Password", type="password", key="reset_password_input")
    if st.button("Reset all votes"):
        if reset_password and reset_password == st.secrets.get("reset_password"):
            reset_all_votes()
            st.success("All votes have been reset.")
            st.rerun()
        else:
            st.error("Incorrect password.")
