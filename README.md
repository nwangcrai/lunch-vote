# Lunch Vote

A tiny Streamlit app for the team to vote on where to get lunch, with live-updating results.

## How it works

- Restaurant options are read from `restaurants.csv` (one name per row) — edit that file to
  change the list, no code changes needed.
- Votes are stored in a local SQLite database (`votes.db`, created automatically on first run).
- Each person can give a 👍 or 👎 to *any number* of restaurants per day (matched by the name
  they type in) — not a single pick. Clicking the same thumb again removes that vote; clicking
  the other thumb switches it.
- Results are always for *today* — the tally naturally resets at midnight since it's filtered by
  date. Past votes stay in the database if you want to look at history later.
- Live results are shown as a diverging bar per restaurant, starting from a center zero-line:
  red extends left by the 👎 count, blue extends right by the 👍 count, with each count labeled
  just outside its bar's tip. A dotted line at the center marks a 0-0 tie. Restaurants with no
  votes yet show no bar. Ranked by net score (👍 − 👎), highest first. Exact counts are also in
  the "Exact counts" expander below the chart.
- The page auto-refreshes every 7 seconds so everyone sees new votes roll in without manually
  reloading.

## Run locally

```
cd lunch_vote
pip install -r requirements.txt
streamlit run app.py
```

Open the printed local URL, and share it with teammates on the same network if you want them to
vote from the same running instance.

## Deploy to Streamlit Community Cloud (free, public URL)

1. Push this folder to a GitHub repo (can be its own repo, unrelated to `casino_code`).
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in, and click "New app".
3. Point it at your repo/branch and set the main file path to `lunch_vote/app.py`.
4. Deploy — you'll get a public URL to share with the team.

### Note on data persistence

Streamlit Community Cloud's filesystem is ephemeral — `votes.db` persists while the app instance
stays running, but a redeploy or app restart (e.g. after inactivity, or a new push) can wipe it.
That's fine for a daily lunch vote since results reset each day anyway, but don't rely on it for
long-term history. If you want durable history later, swap the SQLite connection for a hosted DB
(e.g. Supabase/Postgres) or a Google Sheet — the `get_connection`/`cast_vote`/`get_today_results`
functions in `app.py` are the only places that would need to change.
