import base64
import random
import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

UP_COLOR = "#008300"
UP_COLOR_HOVER = "#006b00"
DOWN_COLOR = "#e34948"
MUTED_COLOR = "#898781"

DB_PATH = Path(__file__).parent / "votes.db"
RESTAURANTS_CSV = Path(__file__).parent / "restaurants.csv"
THUMBS_UP_PNG = Path(__file__).parent / "thumbs_up.png"
THUMBS_DOWN_PNG = Path(__file__).parent / "thumbs_down.png"
BACKUPS_DIR = Path(__file__).parent / "backups"

SENTIMENT_LABELS = {"up": "Thumbs up", "down": "Thumbs down", None: "No opinion"}


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


def submit_votes(voter_name: str, staged_votes: dict[str, str | None]) -> None:
    """Writes a full wizard submission in one pass, diffed against the voter's existing
    votes: unchanged answers are skipped, answers reverted to "no opinion" delete the
    prior row, and new/changed up-down answers upsert. Unlike the old per-click
    cast_vote() (a toggle relative to current DB state), this sets each restaurant to
    exactly what the wizard says, independent of what was there before."""
    conn = get_connection()
    prior_votes = get_my_votes(voter_name)
    restaurant_ids = dict(conn.execute("SELECT name, id FROM restaurants").fetchall())

    for name, sentiment in staged_votes.items():
        prior = prior_votes.get(name)
        if sentiment == prior:
            continue
        restaurant_id = restaurant_ids[name]
        if sentiment is None:
            conn.execute(
                "DELETE FROM votes WHERE voter_name = ? AND restaurant_id = ?",
                (voter_name, restaurant_id),
            )
            _log(conn, voter_name, name, "clear", prior)
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
            _log(conn, voter_name, name, "set", sentiment)

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


init_db()

st.set_page_config(page_title="Lunch Vote", page_icon="\U0001f37d️", layout="wide")

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
    /* Pull the thumbs-up/skip/thumbs-down buttons closer together instead of spanning
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

if "shuffle_seed" not in st.session_state:
    st.session_state["shuffle_seed"] = random.randint(0, 2**31 - 1)

all_restaurants = pd.read_sql_query(
    "SELECT name, description FROM restaurants", get_connection()
)
restaurant_order = list(
    all_restaurants.sample(frac=1, random_state=st.session_state["shuffle_seed"])[
        ["name", "description"]
    ].itertuples(index=False, name=None)
)
num_restaurants = len(restaurant_order)

st.session_state.setdefault("wizard_name_confirmed", False)
st.session_state.setdefault("wizard_step", 0)
st.session_state.setdefault("wizard_submitted", False)
st.session_state.setdefault("staged_votes", {})


def start_wizard(name: str) -> None:
    # Stored separately from the "voter_name" widget key: Streamlit drops widget-backed
    # session_state entries once their widget stops being rendered (the name text_input
    # only renders on the name screen), so reading st.session_state["voter_name"] again
    # from later screens would KeyError.
    prior_votes = get_my_votes(name)
    st.session_state["confirmed_voter_name"] = name
    st.session_state["staged_votes"] = {
        restaurant_name: prior_votes.get(restaurant_name)
        for restaurant_name, _ in restaurant_order
    }
    st.session_state["wizard_name_confirmed"] = True
    st.session_state["wizard_step"] = 0
    st.session_state["wizard_submitted"] = False


if not st.session_state["wizard_name_confirmed"]:
    st.text_input("Your Name", key="voter_name")
    st.caption("Duplicate first names are treated as the same person.")
    if st.button("Start", type="primary"):
        name = st.session_state.get("voter_name", "").strip()
        if name:
            start_wizard(name)
            st.rerun()
        else:
            st.error("Enter your first name to start.")

elif st.session_state["wizard_submitted"]:
    st.success("Thanks! Your votes are recorded.")
    if st.button("Edit my answers again"):
        st.session_state["wizard_submitted"] = False
        st.session_state["wizard_step"] = 0
        st.rerun()

else:
    voter_name = st.session_state["confirmed_voter_name"]

    if st.button("Not you? Change name"):
        st.session_state["wizard_name_confirmed"] = False
        st.rerun()

    step = st.session_state["wizard_step"]

    if step < num_restaurants:
        name, description = restaurant_order[step]
        st.progress((step + 1) / num_restaurants)
        st.caption(f"{step + 1} of {num_restaurants}")
        st.subheader(name)
        st.write(description)

        current = st.session_state["staged_votes"].get(name)

        with st.container(key=f"votebtns_{name}"):
            col_down, col_skip, col_up = st.columns(3)
        if col_down.button(" ", key=f"down_{name}", type="primary" if current == "down" else "secondary"):
            st.session_state["staged_votes"][name] = "down"
            st.rerun()
        if col_skip.button("No opinion", key=f"skip_{name}", type="primary" if current is None else "secondary"):
            st.session_state["staged_votes"][name] = None
            st.rerun()
        if col_up.button(" ", key=f"up_{name}", type="primary" if current == "up" else "secondary"):
            st.session_state["staged_votes"][name] = "up"
            st.rerun()

        col_back, _, col_next = st.columns([1, 3, 1])
        if col_back.button("Back", disabled=step == 0):
            st.session_state["wizard_step"] -= 1
            st.rerun()
        if col_next.button("Next", type="primary"):
            st.session_state["wizard_step"] += 1
            st.rerun()
    else:
        st.subheader("Review your picks")
        for name, _ in restaurant_order:
            sentiment = st.session_state["staged_votes"].get(name)
            st.markdown(f"**{name}** — {SENTIMENT_LABELS[sentiment]}")

        col_back, _, col_submit = st.columns([1, 3, 1])
        if col_back.button("Back"):
            st.session_state["wizard_step"] = num_restaurants - 1
            st.rerun()
        if col_submit.button("Submit", type="primary"):
            submit_votes(voter_name, st.session_state["staged_votes"])
            st.session_state["wizard_submitted"] = True
            st.rerun()

# Hidden from normal visitors: only rendered when the URL has ?admin=1, so the admin
# controls aren't visible in the ordinary voting UI. Still password-gated underneath.
if st.query_params.get("admin") == "1":
    with st.expander("Admin: view results"):
        results_password = st.text_input(
            "Password", type="password", key="results_password_input"
        )
        if results_password:
            if results_password == st.secrets.get("reset_password"):
                results_df = get_results().sort_values("net_score", ascending=False)
                st.caption(f"{get_respondent_count()} respondent(s) so far.")
                st.dataframe(results_df, use_container_width=True)
                st.download_button(
                    "Download results.csv",
                    results_df.to_csv(index=False),
                    file_name="results.csv",
                    mime="text/csv",
                )
            else:
                st.error("Incorrect password.")

    with st.expander("Admin: view / download vote log"):
        st.caption(
            "A CSV backup of current votes is written to backups/ automatically before "
            "any reset, and every vote (and reset) is permanently recorded in the "
            "vote_log table regardless."
        )
        # Read-only, so no RESET confirm phrase needed — just the password. This is what
        # actually lets you recover from an accidental reset: vote_log and the CSV backups
        # only survive an in-app reset, not a Streamlit Cloud redeploy/restart (ephemeral
        # filesystem), so download a copy here before that happens rather than after.
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
