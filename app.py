import base64
import random
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
BACKUPS_DIR = Path(__file__).parent / "backups"


@st.cache_data
def image_data_uri(path: Path) -> str:
    return f"data:image/png;base64,{base64.b64encode(path.read_bytes()).decode()}"


@st.cache_resource
def get_connection() -> sqlite3.Connection:
    """Cached so the whole app (all sessions) shares one open connection instead of
    opening/closing a new one per query, which was the main source of lag under
    concurrent load. WAL mode lets reads proceed without blocking on writes."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
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

    # Append-only audit trail: never dropped/cleared by reset_all_votes (or the schema-drop
    # migration above), so vote history always survives an accidental or malicious reset.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vote_log (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            voter_name TEXT,
            restaurant_name TEXT,
            action TEXT NOT NULL,
            sentiment TEXT
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

    # Restaurants renamed across a restaurants.csv refresh leave a stale duplicate row behind
    # (init_db only upserts, never deletes), which shows up as a second, identical-looking
    # entry in results. Migrate any votes on the old name onto the new one, then drop the
    # stale row, so nobody's vote is lost in the process.
    RENAMED_RESTAURANTS = {"Roti Modern Mediterranean": "Roti"}
    for old_name, new_name in RENAMED_RESTAURANTS.items():
        old_row = conn.execute("SELECT id FROM restaurants WHERE name = ?", (old_name,)).fetchone()
        new_row = conn.execute("SELECT id FROM restaurants WHERE name = ?", (new_name,)).fetchone()
        if old_row and new_row:
            old_id, new_id = old_row[0], new_row[0]
            conn.execute(
                "UPDATE OR IGNORE votes SET restaurant_id = ? WHERE restaurant_id = ?",
                (new_id, old_id),
            )
            conn.execute("DELETE FROM votes WHERE restaurant_id = ?", (old_id,))
            conn.execute("DELETE FROM restaurants WHERE id = ?", (old_id,))

    # Restaurants dropped from restaurants.csv with no rename mapping and no votes attached
    # are safe to prune so they stop cluttering results; ones with votes are left in place
    # rather than silently discarding someone's vote.
    current_names = set(restaurants_df["name"])
    conn.execute(
        f"""
        DELETE FROM restaurants
        WHERE name NOT IN ({",".join("?" * len(current_names))})
        AND id NOT IN (SELECT DISTINCT restaurant_id FROM votes)
        """,
        list(current_names),
    )

    conn.commit()


def _log(conn: sqlite3.Connection, voter_name: str | None, restaurant_name: str | None, action: str, sentiment: str | None) -> None:
    conn.execute(
        "INSERT INTO vote_log (voter_name, restaurant_name, action, sentiment) VALUES (?, ?, ?, ?)",
        (voter_name, restaurant_name, action, sentiment),
    )


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
        _log(conn, voter_name, restaurant_name, "clear", sentiment)
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
        _log(conn, voter_name, restaurant_name, "set", sentiment)
    conn.commit()
    get_results.clear()
    get_respondent_count.clear()
    get_my_votes.clear()


def backup_votes() -> Path:
    """Snapshot the current votes table to a timestamped CSV before any destructive
    operation, so a reset (accidental or malicious) is always recoverable by hand."""
    conn = get_connection()
    df = pd.read_sql_query(
        """
        SELECT v.voter_name, r.name AS restaurant_name, v.sentiment
        FROM votes v JOIN restaurants r ON r.id = v.restaurant_id
        """,
        conn,
    )
    BACKUPS_DIR.mkdir(exist_ok=True)
    backup_path = BACKUPS_DIR / f"votes_{pd.Timestamp.utcnow().strftime('%Y%m%dT%H%M%SZ')}.csv"
    df.to_csv(backup_path, index=False)
    return backup_path


def reset_all_votes() -> Path:
    """Backs up all current votes to CSV and logs the reset (with a full row-by-row
    record in vote_log, which reset never clears) before wiping the votes table."""
    backup_path = backup_votes()
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT v.voter_name, r.name, v.sentiment FROM votes v
        JOIN restaurants r ON r.id = v.restaurant_id
        """
    ).fetchall()
    for voter_name, restaurant_name, sentiment in rows:
        _log(conn, voter_name, restaurant_name, "reset", sentiment)
    conn.execute("DELETE FROM votes")
    conn.commit()
    get_results.clear()
    get_respondent_count.clear()
    get_my_votes.clear()
    return backup_path


@st.cache_data(ttl=5)
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
    return dict(rows)


@st.cache_data(ttl=5)
def get_respondent_count() -> int:
    conn = get_connection()
    return conn.execute("SELECT COUNT(DISTINCT voter_name) FROM votes").fetchone()[0]


@st.cache_data(ttl=5)
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
    df["net_score"] = df["thumbs_up"] - df["thumbs_down"]
    df["total_votes"] = df["thumbs_up"] + df["thumbs_down"]
    return df


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
        padding-left: 10rem;
        padding-right: 10rem;
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
    /* Pull the thumbs-up/thumbs-down buttons closer together instead of spanning
    the full width of their column. */
    [class*="st-key-votebtns_"] div[data-testid="stHorizontalBlock"] {{
        gap: 0.25rem;
        justify-content: center;
    }}
    [class*="st-key-votebtns_"] div[data-testid="stColumn"] {{
        width: fit-content !important;
        flex: none !important;
    }}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("DC Office Lunch Votes")
st.caption(date.today().strftime("%A, %B %d, %Y") + " (by Norman & Alessandro)")

st.text_input("Your Name", key="voter_name")
voter_name = st.session_state["voter_name"]

if not voter_name:
    st.info(
        "Enter your first name above to vote. (Duplicate names are ignored.)",
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

st.subheader("Your Vote & Live Total Results")
results = get_results()
respondent_count = get_respondent_count()
st.caption(
    f"{respondent_count} respondent(s) so far. Bars share one scale. "
    "Restaurant order is randomized per session to avoid bias toward whatever's currently winning."
)

# get_results() is cached and shared across all sessions, so shuffling has to happen here
# (not inside it) or every session would see the same order. Seeding once per session keeps
# the order stable across this session's autorefreshes instead of jumping around every 7s.
if "shuffle_seed" not in st.session_state:
    st.session_state["shuffle_seed"] = random.randint(0, 2**31 - 1)
results = results.sample(frac=1, random_state=st.session_state["shuffle_seed"]).reset_index(drop=True)

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
header_vote.markdown(
    "<div style='text-align:center; transform: translateX(-2px);'>Your Vote</div>",
    unsafe_allow_html=True,
)
header_bar.markdown(centered("All Votes"), unsafe_allow_html=True)
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

    with col_vote:
        with st.container(key=f"votebtns_{name}"):
            col_down, col_up = st.columns(2)
    if col_down.button(" ", key=f"down_{name}", type=down_type, disabled=not voter_name):
        cast_vote(voter_name, name, "down")
        st.rerun()
    if col_up.button(" ", key=f"up_{name}", type=up_type, disabled=not voter_name):
        cast_vote(voter_name, name, "up")
        st.rerun()

if voter_name:
    st.caption("Click a thumb again to remove your vote.")

# Hidden from normal visitors: only rendered when the URL has ?admin=1, so the reset
# control isn't visible in the ordinary voting UI. Still password-gated underneath.
if st.query_params.get("admin") == "1":
    with st.expander("Admin: reset votes"):
        st.caption(
            "A CSV backup of current votes is written to backups/ automatically before "
            "any reset, and every vote (and reset) is permanently recorded in the "
            "vote_log table regardless."
        )
        # The password/confirm fields are keyed on a nonce that bumps after every attempt
        # (success or failure), forcing Streamlit to remount them as brand-new widgets.
        # Deleting the old session_state entry alone isn't enough: the browser keeps
        # showing (and resubmitting) its own last-typed value in the same DOM input, so a
        # correct password typed once could be resubmitted just by clicking the button
        # again with nothing retyped — that's what caused an undesired reset in production.
        if "reset_form_nonce" not in st.session_state:
            st.session_state["reset_form_nonce"] = 0
        nonce = st.session_state["reset_form_nonce"]
        reset_password = st.text_input(
            "Password", type="password", key=f"reset_password_input_{nonce}"
        )
        confirm_phrase = st.text_input(
            "Type RESET to confirm", key=f"reset_confirm_input_{nonce}"
        )
        if st.button("Reset all votes"):
            password_ok = reset_password and reset_password == st.secrets.get("reset_password")
            confirm_ok = confirm_phrase.strip() == "RESET"
            # Bump the nonce so the password/confirm widgets remount blank on the next run
            # (success or not) — but don't st.rerun() here on failure, since that would
            # immediately discard this run's st.error() before the browser ever renders it.
            st.session_state["reset_form_nonce"] += 1
            if password_ok and confirm_ok:
                backup_path = reset_all_votes()
                st.success(f"All votes have been reset. Backup saved to {backup_path.name}.")
                st.rerun()
            elif not password_ok:
                st.error("Incorrect password.")
            else:
                st.error('Type "RESET" exactly to confirm.')

    # Read-only, so no RESET confirm phrase needed — just the password. This is what
    # actually lets you recover from an accidental reset: vote_log and the CSV backups
    # only survive an in-app reset, not a Streamlit Cloud redeploy/restart (ephemeral
    # filesystem), so download a copy here before that happens rather than after.
    with st.expander("Admin: view / download vote log"):
        log_password = st.text_input(
            "Password", type="password", key="log_password_input"
        )
        if log_password:
            if log_password == st.secrets.get("reset_password"):
                conn = get_connection()
                log_df = pd.read_sql_query(
                    "SELECT * FROM vote_log ORDER BY id DESC", conn
                )
                st.caption(f"{len(log_df)} row(s) in vote_log.")
                st.dataframe(log_df, use_container_width=True)
                st.download_button(
                    "Download vote_log.csv",
                    log_df.to_csv(index=False),
                    file_name="vote_log.csv",
                    mime="text/csv",
                )
            else:
                st.error("Incorrect password.")
