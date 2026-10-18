"""
Engagement features — the real interaction signal behind the familiarity score.

The archive (archive.db) holds every message with a timestamp and direction, so
instead of scoring people on channel totals alone, we aggregate what actually
happened per contact:

    eng_active_days   distinct calendar days with at least one message — the
                      core "how much life have you shared" signal (a 3,000-msg
                      week and 3,000 messages across four years look completely
                      different here)
    eng_sent/recv     outbound vs inbound totals → reciprocity (a one-way
                      stream like a newsletter or support bot is not a friend)
    eng_first/last    first and last message ever → tenure and true recency
    eng_recent90      messages in the last 90 days

One GROUP BY pass over archive.db, written onto the contact rows so the score
stays a pure, cheap function of a row (db.strength_breakdown). Refresh runs at
app startup and after every import; contacts whose messages only live as
channel counts (no archived text) keep zeros and the score falls back to
count-based estimates.

The same pass also owns three channels' message counts. WhatsApp's came first
(NC-092), and the rule was then extended to iMessage and LinkedIn (NC-204,
NC-107). For those channels a person's count and
last date mean **the messages the app can actually show you**: the rows in
archive.db for that person on that channel, which is exactly what their timeline
draws, including their lines in group chats. Recounted for every contact, not
only the ones an import touched, and written nowhere else. Before this, each of
the three importers wrote its own measurement:
  WhatsApp   added WhatsApp's own total, then overwrote it with an archive count
             for some people only: some cards disagreed with the timeline.
  iMessage   added Apple's one-to-one totals, which count photos and leave out
             group chats: some cards disagreed.
  LinkedIn   never let the count go down, so it kept placeholders an old scraper
             had counted: some cards claimed messages that were never saved.
An import only adds saved messages (the one thing that removes them is
permanently deleting a person, card and all), so a recount cannot lose history
that a short download window leaves out. That was the job LinkedIn's never-lower
rule did.

CLI:  python crm/engagement.py        # refresh + a small report
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import os
import sys

import config  # noqa: F401 — loads .env
import archive as arc
import db
import figures  # NC-087: the one spelling of a contact-scope filter

RECENT_DAYS = 90

_AGG_SQL = """
SELECT contact_id,
       COUNT(*)                                  AS total,
       SUM(direction)                            AS sent,
       COUNT(DISTINCT substr(ts, 1, 10))         AS active_days,
       MIN(ts)                                   AS first_ts,
       MAX(ts)                                   AS last_ts,
       SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END)  AS recent
FROM messages
GROUP BY contact_id
"""


def compute(arc_conn: Any) -> Dict[int, Dict[str, Any]]:
    """Per-contact engagement features from the archive, keyed by contact_id."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    out: Dict[int, Dict[str, Any]] = {}
    for r in arc_conn.execute(_AGG_SQL, (cutoff,)):
        # A conversation-owned row has no contact to score (NC-111). Left in, it
        # became a phantom entry keyed 0 whose write-back updated nothing — a
        # silent no-op is exactly the kind of thing that goes unnoticed.
        if r["contact_id"] == arc.NO_PERSON:
            continue
        sent = r["sent"] or 0
        out[r["contact_id"]] = {
            "active_days": r["active_days"] or 0,
            "sent": sent,
            "recv": (r["total"] or 0) - sent,
            "recent90": r["recent"] or 0,
            "first": r["first_ts"] or "",
            "last": r["last_ts"] or "",
        }
    return out


# NC-092, NC-204, NC-107. Channels whose per-contact count is a recount of the
# archive, and the two contact columns each is written to. Email, Telegram,
# Messenger and Instagram are not here: their importers still write their own
# counts, which this recount leaves alone.
COUNTED_FROM_ARCHIVE = {
    "whatsapp": ("wa_count", "wa_last"),
    "imessage": ("imsg_count", "imsg_last"),
    "linkedin": ("msg_count", "last_contact"),
}

# One read for all of them, along idx_msg_channel (channel, contact_id).
_CHANNEL_SQL = """
SELECT channel, contact_id, COUNT(*) AS n, MAX(ts) AS last_ts
FROM messages
WHERE channel IN ({channels}) AND contact_id != ?
GROUP BY channel, contact_id
"""


def recount_channels(conn: Any, arc_conn: Any) -> int:
    """Set every contact's COUNTED_FROM_ARCHIVE columns to what the archive holds
    for them: the number of messages and the newest one's time, or 0 and '' when
    it holds none. Only rows that change are written. Returns how many changed.

    Messages that belong to a conversation rather than a person (arc.NO_PERSON)
    are nobody's to count, the same as in compute()."""
    channels = list(COUNTED_FROM_ARCHIVE)
    want: Dict[str, Dict[int, Any]] = {ch: {} for ch in channels}
    sql = _CHANNEL_SQL.format(channels=",".join("?" * len(channels)))
    for r in arc_conn.execute(sql, (*channels, arc.NO_PERSON)):
        want[r["channel"]][r["contact_id"]] = (r["n"], r["last_ts"] or "")
    cols = [c for ch in channels for c in COUNTED_FROM_ARCHIVE[ch]]
    changed = 0
    for row in conn.execute(f"SELECT id, {', '.join(cols)} FROM contacts").fetchall():
        sets, vals = [], []
        for ch in channels:
            count_col, last_col = COUNTED_FROM_ARCHIVE[ch]
            n, last = want[ch].get(row["id"], (0, ""))
            if (row[count_col] or 0, row[last_col] or "") != (n, last):
                sets += [f"{count_col} = ?", f"{last_col} = ?"]
                vals += [n, last]
        if sets:
            conn.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE id = ?",
                         (*vals, row["id"]))
            changed += 1
    return changed


def refresh(conn: Any, archive_path: Optional[str] = None) -> int:
    """Recompute engagement columns for every contact with archived messages,
    and every contact's archive-counted channel columns (recount_channels).
    Returns how many contacts' engagement changed and was written. Missing archive → 0,
    quietly, and nothing is recounted: with no archive to read there is nothing
    to count from, and a wrong path should not zero every card."""
    path = archive_path or arc.ARCHIVE_DB_PATH
    if not os.path.exists(path):
        return 0
    arc_conn = arc.get_archive_db(path)
    try:
        # NC-198. A fresh install's archive.db can exist with no tables in it —
        # something opens it before any import has written a message — and then
        # compute() raised "no such table: messages". Empty is the true answer.
        has_messages = arc_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages'"
        ).fetchone()
        features = compute(arc_conn) if has_messages else {}
        if has_messages:
            db.ensure_columns(conn)
            recount_channels(conn, arc_conn)
            conn.commit()
    finally:
        arc_conn.close()
    if not features:
        return 0
    db.ensure_columns(conn)
    # NC-214. Only the people whose figures changed are written. This used to
    # rewrite every person with a message history on every refresh, and stamp
    # each with the time, so every row changed even when no figure did. The
    # refresh runs at start-up and after every import, so that was the whole
    # contacts table rewritten several times a day for nothing.
    have = {r["id"]: (r["eng_active_days"], r["eng_sent"], r["eng_recv"],
                      r["eng_recent90"], r["eng_first"], r["eng_last"])
            for r in conn.execute(
                """SELECT id, eng_active_days, eng_sent, eng_recv, eng_recent90,
                          eng_first, eng_last FROM contacts""")}
    n = 0
    for cid, f in features.items():
        want = (f["active_days"], f["sent"], f["recv"], f["recent90"],
                f["first"], f["last"])
        if have.get(cid) == want:
            continue
        cur = conn.execute(
            """UPDATE contacts SET
                 eng_active_days = ?, eng_sent = ?, eng_recv = ?,
                 eng_recent90 = ?, eng_first = ?, eng_last = ?,
                 eng_updated_at = datetime('now')
               WHERE id = ?""",
            (*want, cid),
        )
        n += cur.rowcount
    conn.commit()
    return n


def main() -> None:
    db.init_db()
    conn = db.get_db()
    n = refresh(conn)
    print(f"Engagement features refreshed for {n} contacts from the archive.")
    if n:
        top = conn.execute(
            f"""SELECT name, eng_active_days, eng_sent, eng_recv
               FROM contacts WHERE eng_active_days > 0 AND {figures.population_where('visible')}
               ORDER BY eng_active_days DESC LIMIT 5"""
        ).fetchall()
        print("Most shared days:")
        for r in top:
            print(f"  {r['name']}: {r['eng_active_days']} days "
                  f"({r['eng_sent']} sent / {r['eng_recv']} received)")
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
