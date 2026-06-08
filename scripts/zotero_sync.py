# -*- coding: utf-8 -*-
"""
Rebuild a Zotero collection hierarchy as real folders inside a Git repository.

A standard BibLaTeX export is flat: it does not record which collection each item
belongs to. This script reads that information from Zotero's own database
(zotero.sqlite), matches each exported attachment to its item by title, and creates
a Collections/ folder tree that mirrors your Zotero collections.

Input (inside --repo):
  - files/   : attachments from the latest Zotero export ("Export Files" checked)
  - *.bib    : the exported BibLaTeX database
  - a copy of zotero.sqlite (pass with --db)

Output:
  - Collections/<collection>/<subcollection>/...  : PDFs grouped by collection
  - the "file" field of the .bib rewritten to point into Collections/
  - the temporary files/ folder removed

Usage:
  python zotero_sync.py --repo <repo_path> --db <copy_of_zotero.sqlite> [--bib "library.bib"]

Notes:
  - An item that belongs to several collections is copied into each (a filesystem
    cannot reference one file from multiple folders the way Zotero does).
  - Items that cannot be matched go to Collections/(Unfiled)/.
  - No data leaves your machine; only a local copy of the database is read.
"""
import argparse, os, re, json, sqlite3, shutil, unicodedata, sys, glob
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

FORBIDDEN = '<>:"/\\|?*'
UNFILED = "(Unfiled)"
UNSORTED = "(Unsorted attachments)"


def sanitize(name):
    """Make a collection name safe to use as a folder name on all platforms."""
    out = "".join((c if c not in FORBIDDEN else "-") for c in name)
    return out.rstrip(" .")  # Windows: no trailing space or dot


def norm(s):
    """Normalize a title for robust matching (case/accents/punctuation insensitive)."""
    if not s:
        return ""
    s = s.replace("{", "").replace("}", "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# ---------- 1. Read collections from the database ----------
def connect_ro(dbpath):
    """Open zotero.sqlite read-only, ignoring locks held by a running Zotero.

    'immutable=1' tells SQLite the file will not change underneath it, so it
    skips all locking. That lets us read while Zotero has the database open,
    instead of failing with 'database is locked'.
    """
    uri = Path(os.path.abspath(dbpath)).as_uri() + "?immutable=1"
    return sqlite3.connect(uri, uri=True)


def load_title_index(dbpath):
    """Return {normalized_title: [collection_path, ...]} from zotero.sqlite."""
    con = connect_ro(dbpath)
    c = con.cursor()
    c.execute("SELECT collectionID, collectionName, parentCollectionID FROM collections")
    cols = {cid: {"name": name, "parent": parent} for cid, name, parent in c.fetchall()}

    def path_of(cid):
        parts, seen = [], set()
        while cid is not None and cid in cols and cid not in seen:
            seen.add(cid)
            parts.append(cols[cid]["name"])
            cid = cols[cid]["parent"]
        return parts[::-1]

    col_paths = {cid: path_of(cid) for cid in cols}

    c.execute("SELECT fieldID FROM fields WHERE fieldName='title'")
    row = c.fetchone()
    title_fid = row[0] if row else None

    c.execute("""
        SELECT i.itemID, idv.value
        FROM items i
        JOIN itemData idt ON idt.itemID=i.itemID AND idt.fieldID=?
        JOIN itemDataValues idv ON idv.valueID=idt.valueID
    """, (title_fid,))
    titles = {iid: val for iid, val in c.fetchall()}

    c.execute("SELECT itemID FROM deletedItems")
    deleted = {r[0] for r in c.fetchall()}

    c.execute("SELECT collectionID, itemID FROM collectionItems")
    item_cols = {}
    for cid, iid in c.fetchall():
        item_cols.setdefault(iid, []).append(cid)
    con.close()

    ttl_index = {}
    for iid, title in titles.items():
        if iid in deleted:
            continue
        paths = [col_paths[cid] for cid in item_cols.get(iid, []) if cid in cols]
        bucket = ttl_index.setdefault(norm(title), [])
        for p in paths:
            if p not in bucket:
                bucket.append(p)
    return ttl_index


# ---------- 2. Parse the .bib ----------
def parse_bib(bibpath):
    """Return (raw_text, [{title, ntitle, files:[...]}, ...])."""
    with open(bibpath, encoding="utf-8") as f:
        bib = f.read()
    starts = [(m.group(1), m.start()) for m in re.finditer(r"@(\w+)\{[^,]+,", bib)]
    entries = []
    for i, (etype, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(bib)
        block = bib[start:end]
        tm = re.search(r"(?m)^\s*title\s*=\s*\{(.*?)\}\s*,?\s*$", block, re.S)
        fm = re.search(r"(?m)^\s*file\s*=\s*\{(.*?)\}\s*,?\s*$", block, re.S)
        title = tm.group(1).strip() if tm else None
        files = []
        if fm:
            for part in fm.group(1).split(";"):
                pm = re.search(r"(files/[^:]+\.\w+)", part)
                if pm:
                    files.append(pm.group(1).strip())
        entries.append({"title": title, "ntitle": norm(title), "files": files})
    return bib, entries


# ---------- 3. Rebuild ----------
def resolve_bib(repo, bib=None):
    """Return the path to the .bib inside repo, auto-detecting if not given."""
    if bib:
        return os.path.join(repo, bib)
    cands = sorted(glob.glob(os.path.join(repo, "*.bib")))
    if not cands:
        raise FileNotFoundError("No .bib file found at the repository root.")
    return cands[0]


def rebuild(repo, db, bib=None, log=print):
    """Rebuild the Collections/ tree inside `repo` from the Zotero database `db`.

    `log` is a callback taking a single string; defaults to print, but the GUI
    passes its own so output lands in the log pane. Returns a stats dict.
    """
    repo = os.path.abspath(repo)
    filesdir = os.path.join(repo, "files")
    coldir = os.path.join(repo, "Collections")

    bibpath = resolve_bib(repo, bib)
    if not os.path.isfile(bibpath):
        raise FileNotFoundError(f"Bib file not found: {bibpath}")
    if not os.path.isdir(filesdir):
        if os.path.isdir(coldir):
            raise FileNotFoundError(
                "This folder is already sorted (Collections/ exists, no files/ "
                "left to process). To sort again, re-export from Zotero into a "
                "fresh folder with 'Export Files' enabled.")
        raise FileNotFoundError(
            "'files/' not found. Re-export from Zotero first "
            "(with the 'Export Files' option enabled).")
    if not os.path.isfile(db):
        raise FileNotFoundError(f"Database not found: {db}")

    log("Repository : " + repo)
    log("Bib file   : " + os.path.basename(bibpath))

    ttl_index = load_title_index(db)
    bibtext, entries = parse_bib(bibpath)
    log("Collections indexed (by title): " + str(len(ttl_index)))
    log("Bib entries                   : " + str(len(entries)))

    if os.path.isdir(coldir):
        shutil.rmtree(coldir)

    def col_dir(parts):
        return os.path.join(coldir, *[sanitize(p) for p in parts])

    rewrite, copied_src = {}, set()
    copies = matched = 0
    for e in entries:
        paths = ttl_index.get(e["ntitle"], [])
        if e["files"] and paths:
            matched += 1
        if not e["files"]:
            continue
        if not paths:
            paths = [[UNFILED]]
        for relfile in e["files"]:
            src = os.path.join(repo, relfile.replace("/", os.sep))
            if not os.path.exists(src):
                log("  ! missing source: " + relfile)
                continue
            fname = os.path.basename(src)
            first_new = None
            for p in paths:
                d = col_dir(p)
                os.makedirs(d, exist_ok=True)
                dst = os.path.join(d, fname)
                if os.path.exists(dst) and os.path.abspath(dst) != os.path.abspath(src):
                    base, ext = os.path.splitext(fname); k = 2
                    while os.path.exists(os.path.join(d, f"{base} ({k}){ext}")):
                        k += 1
                    dst = os.path.join(d, f"{base} ({k}){ext}")
                shutil.copy2(src, dst)
                copies += 1
                if first_new is None:
                    first_new = os.path.relpath(dst, repo).replace(os.sep, "/")
            rewrite[relfile] = first_new
            copied_src.add(os.path.normpath(src))

    # Attachments present in files/ but not referenced by any .bib entry
    all_files = [os.path.normpath(os.path.join(r, fn))
                 for r, _, fs in os.walk(filesdir) for fn in fs]
    orphans = [f for f in all_files if f not in copied_src]
    if orphans:
        od = os.path.join(coldir, UNSORTED)
        os.makedirs(od, exist_ok=True)
        for o in orphans:
            dst = os.path.join(od, os.path.basename(o))
            if os.path.exists(dst):
                base, ext = os.path.splitext(os.path.basename(o)); k = 2
                while os.path.exists(os.path.join(od, f"{base} ({k}){ext}")):
                    k += 1
                dst = os.path.join(od, f"{base} ({k}){ext}")
            shutil.copy2(o, dst)

    # Rewrite .bib file paths
    n_rw = 0
    for old, new in rewrite.items():
        if old in bibtext:
            bibtext = bibtext.replace(old, new); n_rw += 1
    with open(bibpath, "w", encoding="utf-8") as f:
        f.write(bibtext)

    shutil.rmtree(filesdir)

    stats = {
        "placed": len(copied_src),
        "copies": copies,
        "orphans": len(orphans),
        "rewritten": n_rw,
        "matched": matched,
        "with_files": len([e for e in entries if e["files"]]),
    }
    log("Files placed         : " + str(stats["placed"]))
    log("Copies (incl. multi) : " + str(stats["copies"]))
    log("Unsorted attachments : " + str(stats["orphans"]))
    log("Bib paths rewritten  : " + str(stats["rewritten"]))
    log("Entries matched      : " + f"{stats['matched']} / {stats['with_files']}")
    log("Done.")
    return stats


# ---------- 4. Faithful backup / restore ----------
# A standard export (.bib + Collections/) is great for browsing on GitHub but
# cannot be turned back into a Zotero library with its collection tree, tags and
# notes intact: that information lives only in zotero.sqlite, and the PDFs are
# stored under storage/<KEY>/ keyed by ID, which the export does not preserve.
# To make a faithful restore possible we back up the two files Zotero actually
# needs: the database and the storage/ folder.
BACKUP_DIRNAME = "Zotero-backup"


def _dirsize(path):
    return sum(os.path.getsize(os.path.join(r, f))
               for r, _, fs in os.walk(path) for f in fs)


def _connect_backup_source(db, log=print):
    """Open the live database for a consistent online backup.

    Prefer a normal read-only connection (mode=ro): SQLite's backup API then
    coordinates with a running Zotero and copies a consistent snapshot. Only if
    the database is locked do we fall back to an immutable read (which ignores
    locks but may copy a slightly inconsistent state).
    """
    uri = Path(os.path.abspath(db)).as_uri()
    try:
        con = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=5)
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1")  # force a read lock
        return con
    except sqlite3.OperationalError:
        log("! database busy — using immutable read; close Zotero for a "
            "guaranteed-consistent backup.")
        return connect_ro(db)


def backup_data_dir(db, dest, log=print):
    """Copy zotero.sqlite (+ storage/) into `dest` for a faithful restore.

    The database is copied with SQLite's online backup API, so it produces a
    consistent snapshot even while Zotero is running. storage/ (the PDFs as
    Zotero keeps them) is copied verbatim — this is what makes a real restore
    possible, and it can be large.
    """
    db = os.path.abspath(db)
    if not os.path.isfile(db):
        raise FileNotFoundError("Database not found: " + db)
    datadir = os.path.dirname(db)
    os.makedirs(dest, exist_ok=True)

    log("Backing up database (online snapshot)…")
    src = _connect_backup_source(db, log)
    dst = sqlite3.connect(os.path.join(dest, "zotero.sqlite"))
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()

    storage = os.path.join(datadir, "storage")
    if os.path.isdir(storage):
        target = os.path.join(dest, "storage")
        if os.path.isdir(target):
            shutil.rmtree(target)
        log("Copying storage/ (the PDFs — this can be large)…")
        shutil.copytree(storage, target)
        mb = _dirsize(target) / (1024 * 1024)
        log(f"storage/ copied: {mb:.0f} MB")
    else:
        log("No storage/ next to the database — backing up the .sqlite only.")
    log("Backup written to: " + dest)
    return dest


def _move_aside(path):
    """Rename an existing file/dir to <path>.old-<n> so we never overwrite."""
    if not os.path.exists(path):
        return None
    k = 1
    while os.path.exists(f"{path}.old-{k}"):
        k += 1
    dst = f"{path}.old-{k}"
    shutil.move(path, dst)
    return dst


def restore_data_dir(backup, datadir, log=print):
    """Replace a Zotero data directory with a backup. DESTRUCTIVE.

    The caller MUST make sure Zotero is closed first. Anything already in the
    target is renamed to *.old-<n> rather than deleted, so a mistaken restore
    can be undone by hand. Returns the list of items moved aside.
    """
    bdb = os.path.join(backup, "zotero.sqlite")
    if not os.path.isfile(bdb):
        raise FileNotFoundError("No zotero.sqlite found in backup: " + backup)
    datadir = os.path.abspath(datadir)
    os.makedirs(datadir, exist_ok=True)

    target_db = os.path.join(datadir, "zotero.sqlite")
    # Guard: if the existing DB is locked, Zotero is almost certainly open.
    if os.path.isfile(target_db):
        try:
            con = sqlite3.connect(target_db, timeout=0.5)
            con.execute("BEGIN IMMEDIATE")
            con.rollback()
            con.close()
        except sqlite3.OperationalError:
            raise RuntimeError(
                "The target zotero.sqlite is locked — close Zotero first, "
                "then restore again.")
        except sqlite3.DatabaseError:
            # Not a valid SQLite file (corrupt/placeholder) — restore can still
            # overwrite it; just don't treat the probe failure as fatal.
            pass

    moved = []
    m = _move_aside(target_db)
    if m:
        moved.append(m)
        log("Existing database kept as: " + os.path.basename(m))
    shutil.copy2(bdb, target_db)
    log("Restored zotero.sqlite")

    bstore = os.path.join(backup, "storage")
    if os.path.isdir(bstore):
        target_store = os.path.join(datadir, "storage")
        m = _move_aside(target_store)
        if m:
            moved.append(m)
            log("Existing storage/ kept as: " + os.path.basename(m))
        log("Copying storage/ back…")
        shutil.copytree(bstore, os.path.join(datadir, "storage"))
        log("Restored storage/")
    else:
        log("Backup has no storage/ — database restored without PDFs.")
    log("Restore complete. Reopen Zotero.")
    return moved


def main():
    ap = argparse.ArgumentParser(description="Mirror Zotero collections as folders.")
    ap.add_argument("--repo", required=True, help="Path to the library repository.")
    ap.add_argument("--db", required=True, help="Path to a copy of zotero.sqlite.")
    ap.add_argument("--bib", default=None, help="Name of the .bib (auto-detected otherwise).")
    ap.add_argument("--backup", action="store_true",
                    help="Also copy the database + storage/ into the repo for a "
                         "faithful restore.")
    args = ap.parse_args()
    try:
        rebuild(args.repo, args.db, args.bib)
        if args.backup:
            backup_data_dir(args.db,
                            os.path.join(os.path.abspath(args.repo), BACKUP_DIRNAME))
    except (FileNotFoundError, RuntimeError) as ex:
        print("ERROR:", ex, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
