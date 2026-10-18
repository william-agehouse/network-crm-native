"""
Local photo storage for contacts — files in data/photos/, metadata on the contact row.

Faces make every list, review card, and profile feel personal, so importers try hard
to find one — but the storage rules stay boring and safe:

  • One file per contact: data/photos/<id>.jpg (gitignored with the rest of data/).
  • Source priority: a photo you set by hand is never clobbered by an import, and
    below that, LinkedIn wins.
        manual (4) > linkedin (3) > contacts (2) > whatsapp / google (1)
    An equal-rank re-import MAY refresh the file (people change their avatars).

    LINKEDIN OVER EVERYTHING AUTOMATIC:
    LinkedIn pictures rank highest, above the address book and above pictures from
    personal chats, which is what this app is FOR. A LinkedIn photo is a portrait the person chose
    to represent themselves professionally; a WhatsApp or iMessage avatar is
    whatever they felt like that month — a holiday snap, their dog, a baby, a
    group in-joke — and an address-book photo is often a picture YOU took of them
    at a party. In a tool you open to remember who someone is before you talk to
    them, the professional headshot is the one that identifies them.
    `manual` deliberately stays on top: if you set a face by hand, that is a
    decision, and no import gets to overrule it (invariant 5).
  • Images are resized to ≤512px via macOS `sips` when available; otherwise stored
    as-is. Only JPEG/PNG/HEIC magic bytes are accepted. Nothing is ever fetched from
    the network here — callers hand us bytes they read from files the user provided.
"""

import hashlib
import os
import subprocess
from typing import Any, Optional

import config

PHOTOS_DIR = os.environ.get("CRM_PHOTOS_DIR", config.data_path("photos"))
MAX_DIM = 512
MIN_BYTES = 120          # anything smaller is a placeholder/corrupt blob
MAX_BYTES = 20_000_000   # sanity cap

# See the module docstring for why linkedin outranks contacts and the chat sources.
SOURCE_RANK = {"manual": 4, "linkedin": 3, "contacts": 2, "whatsapp": 1, "google": 1}


def source_rank(source: str) -> int:
    return SOURCE_RANK.get((source or "").strip().lower(), 0)


def sniff_content_type(data: bytes) -> Optional[str]:
    """Image content-type from magic bytes, or None if it isn't an image we accept."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"\x89PNG":
        return "image/png"
    if len(data) > 12 and data[4:8] == b"ftyp":  # HEIC/HEIF container
        return "image/heic"
    return None


def photo_path(contact_id: int) -> str:
    return os.path.join(PHOTOS_DIR, f"{int(contact_id)}.jpg")


def photo_url(row: Any) -> str:
    """Cache-busted URL for a contact row, or '' if it has no stored photo.

    The ?v= is the file mtime, so replacing a photo invalidates browser caches
    without any bookkeeping."""
    try:
        if not row["photo"]:
            return ""
        path = photo_path(row["id"])
        return f"/api/photo/{row['id']}?v={int(os.stat(path).st_mtime)}"
    except (OSError, KeyError, IndexError):
        return ""


def _normalize_image(data: bytes, dest: str) -> bool:
    """Write `data` to `dest` as a ≤MAX_DIM jpeg. Uses macOS sips when present
    (also converts PNG/HEIC → JPEG); falls back to writing JPEG/PNG bytes as-is.
    Returns False when the bytes can't be stored (e.g. HEIC without sips)."""
    ctype = sniff_content_type(data)
    if ctype is None:
        return False
    tmp = dest + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    try:
        r = subprocess.run(
            ["sips", "-s", "format", "jpeg", "-Z", str(MAX_DIM), tmp, "--out", dest],
            capture_output=True, timeout=30,
        )
        if r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) >= MIN_BYTES:
            os.remove(tmp)
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    # No sips (or it failed): keep raw bytes if they're browser-renderable.
    if ctype == "image/heic":
        os.remove(tmp)
        return False
    os.replace(tmp, dest)
    return True


def _same_image_on_another_contact(conn: Any, contact_id: int, data: bytes) -> int:
    """The id of a DIFFERENT contact already holding this exact image, or 0.

    Compares file SIZE first and only hashes the handful of files that match, so the
    cost is a stat() per photo rather than a read of all of them. Across a few hundred
    photos on disk a size collision is rare, and an identical image is a size collision by
    definition — so this is exact, not heuristic.

    Deliberately compares the stored file rather than a hash column: no schema
    change, and it works on photos that were saved long before this guard existed,
    which is the whole population that needs protecting.
    """
    want = len(data)
    try:
        digest = hashlib.sha256(data).hexdigest()
    except Exception:      # noqa: BLE001 — a guard must never break the save path
        return 0
    rows = conn.execute(
        "SELECT id, photo FROM contacts WHERE photo != '' AND id != ?",
        (int(contact_id),),
    ).fetchall()
    for r in rows:
        path = os.path.join(PHOTOS_DIR, str(r["photo"]))
        try:
            if os.path.getsize(path) != want:
                continue
            with open(path, "rb") as fh:
                if hashlib.sha256(fh.read()).hexdigest() == digest:
                    return int(r["id"])
        except OSError:
            continue           # a missing or unreadable file cannot be a clash
    return 0


def save_photo(conn: Any, contact_id: int, data: bytes, source: str) -> bool:
    """Store a photo for a contact, honoring source priority. Returns True if saved.

    A lower-ranked source never replaces a higher-ranked photo; an equal or higher
    rank refreshes it. The contact row records the filename + winning source."""
    if not data or len(data) < MIN_BYTES or len(data) > MAX_BYTES:
        return False
    row = conn.execute(
        "SELECT id, photo, photo_source FROM contacts WHERE id = ?", (int(contact_id),)
    ).fetchone()
    if row is None:
        return False
    if row["photo"] and source_rank(source) < source_rank(row["photo_source"]):
        return False
    # ONE FACE, ONE PERSON. A photo that is byte-identical to another contact's is
    # refused for every automatic source.
    #
    # This is the second line of defence against a class of bug that is easy to
    # write: a scraper that searches a whole profile page for a display-photo URL
    # picks up the VIEWER's own avatar, which LinkedIn puts in the global nav under
    # the same pattern, so every profile whose person had no photo of their own
    # would get the scraping account's face. The scraper avoids it, but the same
    # shape of mistake is available to any importer that mis-attributes an image —
    # a shared WhatsApp group icon, a default avatar, a mismatched vCard — so the
    # guard belongs here, where every source funnels through, rather than only in
    # the scraper.
    #
    # 'manual' is exempt: if the owner deliberately sets the same picture on two
    # people, that is their call and this function has no business overruling it.
    if (source or "").strip().lower() != "manual":
        clash = _same_image_on_another_contact(conn, int(contact_id), data)
        if clash:
            return False
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    dest = photo_path(contact_id)
    if not _normalize_image(data, dest):
        return False
    conn.execute(
        "UPDATE contacts SET photo = ?, photo_source = ?, updated_at = datetime('now') WHERE id = ?",
        (os.path.basename(dest), (source or "").strip().lower(), int(contact_id)),
    )
    return True


def delete_photo(conn: Any, contact_id: int) -> None:
    """Remove a contact's photo file and clear the row metadata."""
    try:
        os.remove(photo_path(contact_id))
    except OSError:
        pass
    conn.execute(
        "UPDATE contacts SET photo = '', photo_source = '', updated_at = datetime('now') WHERE id = ?",
        (int(contact_id),),
    )


def read_photo(contact_id: int) -> Optional[bytes]:
    """The stored photo bytes, or None."""
    try:
        with open(photo_path(contact_id), "rb") as fh:
            return fh.read()
    except OSError:
        return None


def move_photo(conn: Any, from_id: int, to_id: int) -> None:
    """Merge helper: give `to_id` the photo of `from_id` if it doesn't have a
    better one already (used by db.merge_contacts)."""
    src = conn.execute(
        "SELECT photo, photo_source FROM contacts WHERE id = ?", (int(from_id),)
    ).fetchone()
    dst = conn.execute(
        "SELECT photo, photo_source FROM contacts WHERE id = ?", (int(to_id),)
    ).fetchone()
    if src is None or dst is None or not src["photo"]:
        return
    if dst["photo"] and source_rank(dst["photo_source"]) >= source_rank(src["photo_source"]):
        return
    try:
        os.replace(photo_path(from_id), photo_path(to_id))
    except OSError:
        return
    conn.execute(
        "UPDATE contacts SET photo = ?, photo_source = ? WHERE id = ?",
        (os.path.basename(photo_path(to_id)), src["photo_source"], int(to_id)),
    )
