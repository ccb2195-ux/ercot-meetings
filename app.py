import csv
import io
import json
import os
import sqlite3
import zipfile
from datetime import date, timedelta

from flask import Flask, Response, g, redirect, render_template, request, send_from_directory, url_for

from newcomers import find_newcomers

app = Flask(__name__)
DB_PATH = os.environ.get("DB_PATH", "ercot.db")
SERVE_LOCAL = (
    os.environ.get("SERVE_LOCAL_DOCS", "true").lower() == "true"
    and os.path.isdir("docs")
)


AP_MONTHS = ["Jan.", "Feb.", "March", "April", "May", "June",
             "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec."]

# Searches suggested on the homepage. Counts are computed live.
SUGGESTED_SEARCHES = [
    "data center", "large load", "Data Center Coalition",
    "NVIDIA", "crypto", "nuclear", "winter storm", "hydrogen",
]


@app.template_filter("apdate")
def apdate(value, short=False):
    """'2026-07-09' -> 'July 9, 2026' (AP style). short=True drops the year."""
    try:
        y, m, d = (int(x) for x in str(value)[:10].split("-"))
    except (ValueError, TypeError):
        return value or ""
    out = f"{AP_MONTHS[m - 1]} {d}"
    return out if short else f"{out}, {y}"


@app.template_filter("apmonth")
def apmonth(value):
    """'2026-07' -> 'July 2026'."""
    try:
        y, m = (int(x) for x in str(value)[:7].split("-"))
    except (ValueError, TypeError):
        return value or ""
    return f"{AP_MONTHS[m - 1]} {y}"


@app.template_filter("filesize")
def filesize(n):
    if not n:
        return "—"
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"


@app.template_filter("etype")
def etype_label(t, plural=False):
    labels = {"PERSON": ("Person", "People"), "ORG": ("Organization", "Organizations"),
              "NPRR": ("NPRR", "NPRRs")}
    pair = labels.get(t, ("Entity", "Entities"))
    return pair[1] if plural else pair[0]


def latest_data_date(db):
    """Date of the newest meeting that actually has files.

    The calendar scrape also picks up meetings scheduled later in the month,
    which have no documents yet. Counting those would make the site claim
    data it doesn't have and drag every chart's last month toward zero."""
    try:
        d = db.execute("SELECT MAX(meeting_date) FROM documents").fetchone()[0]
        if not d:
            d = db.execute("SELECT MAX(date) FROM meetings").fetchone()[0]
        return d[:10] if d else None
    except sqlite3.Error:
        return None


@app.context_processor
def inject_globals():
    return {"data_through": latest_data_date(get_db()), "serve_local": SERVE_LOCAL,
            "today": date.today().isoformat()}


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("""CREATE TABLE IF NOT EXISTS entity_blocklist (
            entity_text TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            PRIMARY KEY (entity_text, entity_type)
        )""")
        g.db.commit()
    return g.db


@app.teardown_appcontext
def close_db(error):
    db = g.pop("db", None)
    if db:
        db.close()


@app.route("/")
def index():
    db = get_db()
    recent = db.execute(
        """SELECT m.*, COUNT(d.id) as doc_count
           FROM meetings m
           LEFT JOIN documents d ON d.meeting_url = m.url
           WHERE m.status != 'cancelled'
           GROUP BY m.url
           HAVING doc_count > 0
           ORDER BY m.date DESC
           LIMIT 8"""
    ).fetchall()
    stats = {
        "meetings":   db.execute("SELECT COUNT(*) FROM meetings").fetchone()[0],
        "documents":  db.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
        "committees": db.execute("SELECT COUNT(DISTINCT committee) FROM meetings").fetchone()[0],
        "minutes":    db.execute("SELECT COUNT(*) FROM documents WHERE looks_like_minutes=1").fetchone()[0],
    }
    type_counts = db.execute(
        "SELECT url_ext, COUNT(*) as n FROM documents GROUP BY url_ext ORDER BY n DESC"
    ).fetchall()
    top_committees = db.execute(
        """SELECT committee, COUNT(*) as n FROM meetings
           GROUP BY committee ORDER BY n DESC LIMIT 8"""
    ).fetchall()
    committees = db.execute(
        "SELECT DISTINCT committee FROM meetings ORDER BY committee"
    ).fetchall()
    years = db.execute(
        "SELECT DISTINCT substr(date,1,4) as yr FROM meetings ORDER BY yr DESC"
    ).fetchall()

    # Entity visualizations — only if entities table exists and has data
    has_entities = db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='entities'"
    ).fetchone()[0]

    top_people = top_orgs = []
    org_trend = people_trend = {"months": [], "series": []}

    if has_entities:
        top_people = db.execute(
            """SELECT e.entity_text, COUNT(DISTINCT e.document_id) as doc_count
               FROM entities e
               WHERE e.entity_type='PERSON'
                 AND NOT EXISTS (SELECT 1 FROM entity_blocklist bl
                     WHERE bl.entity_text=e.entity_text AND bl.entity_type=e.entity_type)
               GROUP BY e.entity_text ORDER BY doc_count DESC LIMIT 12"""
        ).fetchall()

        top_orgs = db.execute(
            """SELECT e.entity_text, COUNT(DISTINCT e.document_id) as doc_count
               FROM entities e
               WHERE e.entity_type='ORG'
                 AND NOT EXISTS (SELECT 1 FROM entity_blocklist bl
                     WHERE bl.entity_text=e.entity_text AND bl.entity_type=e.entity_type)
               GROUP BY e.entity_text ORDER BY doc_count DESC LIMIT 12"""
        ).fetchall()

        # Build the 6-month window ending at the LATEST MONTH IN THE DATA,
        # not today's date. The DB is published monthly and can lag the
        # calendar, which used to leave the trend charts mostly empty.
        latest = latest_data_date(db)
        anchor = date.fromisoformat(latest[:10]) if latest else date.today()
        # If the newest month is only partly scraped, end on the month before
        # so the trend lines don't all fake a nosedive.
        if anchor.day < 25:
            anchor = anchor.replace(day=1) - timedelta(days=1)
        months = []
        for i in range(5, -1, -1):
            m, y = anchor.month - i, anchor.year
            while m <= 0:
                m += 12; y -= 1
            months.append(f"{y}-{m:02d}")
        cutoff = months[0] + "-01"

        # Top 5 orgs by doc count *within* the 6-month window
        top5_rows = db.execute(
            """SELECT e.entity_text
               FROM entities e
               JOIN documents d ON d.id = e.document_id
               WHERE e.entity_type = 'ORG'
                 AND d.meeting_date >= ?
                 AND NOT EXISTS (SELECT 1 FROM entity_blocklist bl
                     WHERE bl.entity_text=e.entity_text AND bl.entity_type=e.entity_type)
               GROUP BY e.entity_text
               ORDER BY COUNT(DISTINCT e.document_id) DESC
               LIMIT 5""",
            [cutoff],
        ).fetchall()
        top5_names = [r["entity_text"] for r in top5_rows]

        # Monthly doc counts for those orgs
        org_trend = {"months": months, "series": []}
        if top5_names:
            placeholders = ",".join("?" * len(top5_names))
            raw_trend = db.execute(
                f"""SELECT e.entity_text,
                           substr(d.meeting_date, 1, 7) as month,
                           COUNT(DISTINCT e.document_id) as doc_count
                    FROM entities e
                    JOIN documents d ON d.id = e.document_id
                    WHERE e.entity_type = 'ORG'
                      AND d.meeting_date >= ?
                      AND e.entity_text IN ({placeholders})
                    GROUP BY e.entity_text, month""",
                [cutoff] + top5_names,
            ).fetchall()
            trend_map = {name: {m: 0 for m in months} for name in top5_names}
            for r in raw_trend:
                if r["entity_text"] in trend_map and r["month"] in trend_map[r["entity_text"]]:
                    trend_map[r["entity_text"]][r["month"]] = r["doc_count"]
            org_trend["series"] = [
                {"name": name, "data": [trend_map[name][m] for m in months]}
                for name in top5_names
            ]

        # Top 5 people by doc count within the 6-month window
        top5_ppl_rows = db.execute(
            """SELECT e.entity_text
               FROM entities e
               JOIN documents d ON d.id = e.document_id
               WHERE e.entity_type = 'PERSON'
                 AND d.meeting_date >= ?
                 AND NOT EXISTS (SELECT 1 FROM entity_blocklist bl
                     WHERE bl.entity_text=e.entity_text AND bl.entity_type=e.entity_type)
               GROUP BY e.entity_text
               ORDER BY COUNT(DISTINCT e.document_id) DESC
               LIMIT 5""",
            [cutoff],
        ).fetchall()
        top5_ppl_names = [r["entity_text"] for r in top5_ppl_rows]

        people_trend = {"months": months, "series": []}
        if top5_ppl_names:
            placeholders_ppl = ",".join("?" * len(top5_ppl_names))
            raw_ppl = db.execute(
                f"""SELECT e.entity_text,
                           substr(d.meeting_date, 1, 7) as month,
                           COUNT(DISTINCT e.document_id) as doc_count
                    FROM entities e
                    JOIN documents d ON d.id = e.document_id
                    WHERE e.entity_type = 'PERSON'
                      AND d.meeting_date >= ?
                      AND e.entity_text IN ({placeholders_ppl})
                    GROUP BY e.entity_text, month""",
                [cutoff] + top5_ppl_names,
            ).fetchall()
            ppl_map = {name: {m: 0 for m in months} for name in top5_ppl_names}
            for r in raw_ppl:
                if r["entity_text"] in ppl_map and r["month"] in ppl_map[r["entity_text"]]:
                    ppl_map[r["entity_text"]][r["month"]] = r["doc_count"]
            people_trend["series"] = [
                {"name": name, "data": [ppl_map[name][m] for m in months]}
                for name in top5_ppl_names
            ]

    # Monthly volume for the homepage strip chart: 24 consecutive months
    # ending with the latest month that has files. Empty months stay in
    # as zero-height bars so gaps in the record are visible, not hidden.
    through = latest_data_date(db) or date.today().isoformat()
    end = date.fromisoformat(through[:10])
    first = db.execute("SELECT MIN(date) FROM meetings").fetchone()[0]
    span = []
    y, m = end.year, end.month
    for _ in range(24):
        key = f"{y}-{m:02d}"
        if first and key < first[:7]:
            break
        span.append(key)
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    span.reverse()
    counts = {r["month"]: r for r in db.execute(
        """SELECT substr(m.date,1,7) AS month,
                  COUNT(DISTINCT m.url) AS meetings,
                  COUNT(d.id)           AS docs
           FROM meetings m
           LEFT JOIN documents d ON d.meeting_url = m.url
           WHERE m.date >= ? AND m.date <= ?
           GROUP BY month""",
        ((span[0] + "-01") if span else "0000", through),
    ).fetchall()}
    monthly = [{"month": k,
                "meetings": counts[k]["meetings"] if k in counts else 0,
                "docs": counts[k]["docs"] if k in counts else 0} for k in span]

    # Suggested searches with live hit counts (FTS only)
    suggestions = []
    if fts_available(db):
        for term in SUGGESTED_SEARCHES:
            fts_q = f'"{term}"'
            try:
                n = db.execute(
                    "SELECT COUNT(*) FROM document_fts WHERE document_fts MATCH ?", (fts_q,)
                ).fetchone()[0]
            except sqlite3.Error:
                n = 0
            if n:
                suggestions.append({"term": term, "q": fts_q if " " in term else term, "n": n})

    latest_month = monthly[-1]["month"] if monthly else None

    newcomers = None
    if has_entities:
        try:
            newcomers = find_newcomers(db, DB_PATH, window_days=90, n_orgs=3, n_rules=3)
        except sqlite3.Error:
            newcomers = None

    return render_template("index.html", recent=recent, stats=stats,
                           type_counts=type_counts, top_committees=top_committees,
                           committees=committees, years=years,
                           top_people=top_people, top_orgs=top_orgs,
                           org_trend=org_trend, people_trend=people_trend,
                           has_entities=has_entities, monthly=monthly,
                           suggestions=suggestions, latest_month=latest_month,
                           newcomers=newcomers)


def fts_available(db):
    return bool(db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='document_fts'"
    ).fetchone())


def _has_table(db, name):
    return bool(db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


@app.route("/search")
def search():
    q           = request.args.get("q", "").strip()
    type_filter = request.args.get("type", "").strip()
    committee   = request.args.get("committee", "").strip()
    results     = []
    used_fts    = False

    db = get_db()
    all_types      = db.execute("SELECT DISTINCT url_ext FROM documents ORDER BY url_ext").fetchall()
    all_committees = db.execute("SELECT DISTINCT committee FROM meetings ORDER BY committee").fetchall()

    if q or type_filter or committee:
        extra_clauses = []
        extra_params  = []
        if type_filter:
            extra_clauses.append("d.url_ext = ?")
            extra_params.append(type_filter)
        if committee:
            extra_clauses.append("d.committee = ?")
            extra_params.append(committee)
        extra_where = (" AND " + " AND ".join(extra_clauses)) if extra_clauses else ""

        if q and fts_available(db):
            # Full-text search — works with both full DB (snippet) and slim DB (teaser)
            used_fts = True
            if _has_table(db, "document_text"):
                # Local full DB: show highlighted match snippet
                snippet_expr = "snippet(document_fts, 0, '<mark>', '</mark>', '…', 25)"
                text_join    = "JOIN document_text dt ON document_fts.rowid = dt.document_id"
            elif _has_table(db, "document_summary"):
                # Slim public DB: show 300-char teaser instead of match snippet
                snippet_expr = "ds.teaser"
                text_join    = "JOIN document_summary ds ON document_fts.rowid = ds.document_id"
            else:
                snippet_expr = "''"
                text_join    = ""

            used_fts = True
            sql = f"""SELECT d.*,
                           m.title AS meeting_title,
                           {snippet_expr} AS snippet
                    FROM document_fts
                    {text_join}
                    JOIN documents d ON d.id = document_fts.rowid
                    JOIN meetings m  ON m.url = d.meeting_url
                    WHERE document_fts MATCH ?
                    {extra_where}
                    ORDER BY rank
                    LIMIT 200"""
            try:
                results = db.execute(sql, [q] + extra_params).fetchall()
            except sqlite3.OperationalError:
                # Punctuation like - : ' ( breaks FTS5 query syntax.
                # Retry the whole thing as a literal phrase.
                safe_q = '"' + q.replace('"', '""') + '"'
                results = db.execute(sql, [safe_q] + extra_params).fetchall()
        else:
            # Fallback: title-only LIKE search
            clauses = []
            params  = []
            if q:
                clauses.append("d.doc_title LIKE ?")
                params.append(f"%{q}%")
            clauses.extend(extra_clauses)
            params.extend(extra_params)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            results = db.execute(
                f"""SELECT d.*, m.title AS meeting_title, '' AS snippet
                    FROM documents d
                    JOIN meetings m ON d.meeting_url = m.url
                    {where}
                    ORDER BY d.meeting_date DESC
                    LIMIT 200""",
                params,
            ).fetchall()

    return render_template("search.html", q=q, results=results,
                           type_filter=type_filter, committee=committee,
                           all_types=all_types, all_committees=all_committees,
                           used_fts=used_fts)


@app.route("/meetings")
def meetings():
    db         = get_db()
    committee  = request.args.get("committee", "")
    year       = request.args.get("year", "")

    clauses = []
    params  = []
    if committee:
        clauses.append("m.committee = ?")
        params.append(committee)
    if year:
        clauses.append("m.date LIKE ?")
        params.append(f"{year}%")

    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = db.execute(
        f"""SELECT m.*, COUNT(d.id) as doc_count
            FROM meetings m
            LEFT JOIN documents d ON d.meeting_url = m.url
            {where}
            GROUP BY m.url
            ORDER BY m.date DESC""",
        params,
    ).fetchall()

    committees = db.execute(
        "SELECT DISTINCT committee FROM meetings ORDER BY committee"
    ).fetchall()
    years = db.execute(
        "SELECT DISTINCT substr(date,1,4) as yr FROM meetings ORDER BY yr DESC"
    ).fetchall()

    month_counts = {}
    for r in rows:
        month_counts[r["date"][:7]] = month_counts.get(r["date"][:7], 0) + 1

    return render_template("meetings.html", meetings=rows, committees=committees,
                           years=years, selected=committee, selected_year=year,
                           month_counts=month_counts)


@app.route("/doc/<int:doc_id>")
def doc(doc_id):
    db  = get_db()
    d   = db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not d:
        return "Document not found", 404
    mtg = db.execute("SELECT * FROM meetings WHERE url = ?", (d["meeting_url"],)).fetchone()
    if _has_table(db, "document_text"):
        txt = db.execute(
            "SELECT text FROM document_text WHERE document_id = ?", (doc_id,)
        ).fetchone()
        raw_text = txt["text"] if txt else None
    elif _has_table(db, "document_summary"):
        txt = db.execute(
            "SELECT teaser FROM document_summary WHERE document_id = ?", (doc_id,)
        ).fetchone()
        raw_text = txt["teaser"] if txt else None
    else:
        raw_text = None

    # Split OCR section out if present
    body_text, ocr_text = raw_text, None
    if raw_text and "[OCR from slide graphics]" in raw_text:
        parts     = raw_text.split("[OCR from slide graphics]", 1)
        body_text = parts[0].strip()
        ocr_text  = parts[1].strip()

    ents = db.execute(
        """SELECT entity_text, entity_type, count
           FROM entities e WHERE document_id = ?
             AND NOT EXISTS (SELECT 1 FROM entity_blocklist bl
                 WHERE bl.entity_text=e.entity_text AND bl.entity_type=e.entity_type)
           ORDER BY entity_type, count DESC""",
        (doc_id,),
    ).fetchall()

    return render_template("doc.html", doc=d, meeting=mtg,
                           body_text=body_text, ocr_text=ocr_text, entities=ents,
                           serve_local=SERVE_LOCAL)


@app.route("/meeting/<path:slug>")
def meeting(slug):
    db  = get_db()
    url = f"https://www.ercot.com/calendar/{slug}"
    mtg = db.execute("SELECT * FROM meetings WHERE url = ?", (url,)).fetchone()
    if not mtg:
        return "Meeting not found", 404
    docs = db.execute(
        "SELECT * FROM documents WHERE meeting_url = ? ORDER BY url_ext, doc_title",
        (url,),
    ).fetchall()

    # Natural sort so agenda numbers run 1, 2, ... 10 instead of 1, 10, 11, 2
    import re
    def natural(d):
        return [int(t) if t.isdigit() else t.lower()
                for t in re.split(r"(\d+)", d["doc_title"] or "")]
    docs = sorted(docs, key=lambda d: (d["url_ext"], natural(d)))

    # Group docs by type for display
    by_type = {}
    for d in docs:
        by_type.setdefault(d["url_ext"].upper(), []).append(d)

    return render_template("meeting.html", meeting=mtg, docs=docs, by_type=by_type)


def build_filter_query(prefix="d"):
    """Build WHERE clause and params from shared filter query args."""
    clauses, params = [], []
    committee = request.args.get("committee", "")
    year      = request.args.get("year", "")
    doc_type  = request.args.get("type", "")
    minutes   = request.args.get("minutes", "")
    if committee:
        clauses.append(f"{prefix}.committee = ?"); params.append(committee)
    if year:
        clauses.append(f"{prefix}.meeting_date LIKE ?"); params.append(f"{year}%")
    if doc_type:
        clauses.append(f"{prefix}.url_ext = ?"); params.append(doc_type)
    if minutes:
        clauses.append(f"{prefix}.looks_like_minutes = 1")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


@app.route("/download/count")
def download_count():
    db = get_db()
    where, params = build_filter_query()
    n = db.execute(
        f"SELECT COUNT(*) FROM documents d {where}", params
    ).fetchone()[0]
    return {"count": n}


@app.route("/download/csv")
def download_csv():
    db    = get_db()
    where, params = build_filter_query()
    rows  = db.execute(
        f"""SELECT d.doc_title, d.doc_url, d.meeting_date, d.committee,
                   d.url_ext, d.size_bytes, d.looks_like_minutes,
                   d.meeting_url, d.extraction_status
            FROM documents d {where}
            ORDER BY d.meeting_date DESC, d.committee, d.doc_title""",
        params,
    ).fetchall()

    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(["doc_title", "doc_url", "meeting_date", "committee",
                "type", "size_bytes", "looks_like_minutes",
                "meeting_url", "extraction_status"])
    for r in rows:
        w.writerow(list(r))

    fname = "ercot_documents.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


@app.route("/download/zip")
def download_zip():
    db    = get_db()
    where, params = build_filter_query()
    rows  = db.execute(
        f"""SELECT d.filename, d.doc_title, d.url_ext
            FROM documents d {where}
            ORDER BY d.meeting_date DESC""",
        params,
    ).fetchall()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in rows:
            src = os.path.join("docs", r["filename"])
            if os.path.exists(src):
                # Use readable name inside the ZIP
                safe_title = "".join(
                    c if c.isalnum() or c in " -_()" else "_"
                    for c in r["doc_title"]
                )[:100]
                zf.write(src, f"{safe_title}.{r['url_ext']}")
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": "attachment; filename=ercot_documents.zip"},
    )


@app.route("/entities")
def entities():
    db          = get_db()
    entity_type = request.args.get("type", "PERSON")
    q           = request.args.get("q", "").strip()

    clauses, params = ["e.entity_type = ?"], [entity_type]
    if q:
        clauses.append("e.entity_text LIKE ?")
        params.append(f"%{q}%")
    clauses.append("""NOT EXISTS (SELECT 1 FROM entity_blocklist bl
                       WHERE bl.entity_text=e.entity_text AND bl.entity_type=e.entity_type)""")
    where = "WHERE " + " AND ".join(clauses)

    rows = db.execute(
        f"""SELECT e.entity_text, e.entity_type,
                   COUNT(DISTINCT e.document_id) as doc_count,
                   SUM(e.count) as mention_count
            FROM entities e
            {where}
            GROUP BY e.entity_text, e.entity_type
            ORDER BY doc_count DESC
            LIMIT 150""",
        params,
    ).fetchall()

    type_counts = db.execute(
        """SELECT entity_type,
                  COUNT(DISTINCT entity_text) as unique_count,
                  COUNT(DISTINCT document_id) as doc_count
           FROM entities GROUP BY entity_type ORDER BY doc_count DESC"""
    ).fetchall()

    return render_template("entities.html", entities=rows,
                           type_counts=type_counts, selected_type=entity_type, q=q,
                           admin_mode=SERVE_LOCAL)


@app.route("/entity/<path:name>")
def entity(name):
    db          = get_db()
    entity_type = request.args.get("type", "")

    docs = db.execute(
        """SELECT d.*, e.count as mention_count, e.entity_type,
                  m.title as meeting_title
           FROM entities e
           JOIN documents d ON d.id = e.document_id
           JOIN meetings m  ON m.url = d.meeting_url
           WHERE e.entity_text = ?
             AND (? = '' OR e.entity_type = ?)
           ORDER BY d.meeting_date DESC""",
        (name, entity_type, entity_type),
    ).fetchall()

    # Docs over time across the whole archive, for the timeline strip.
    # Monthly bars while that fits; quarterly or yearly once backfills
    # stretch the archive past ~3 years, so bars stay readable. The range is
    # continuous, so months with no meetings show as gaps instead of vanishing.
    through = latest_data_date(db) or date.today().isoformat()
    first = db.execute("SELECT MIN(date) FROM meetings").fetchone()[0] or through
    all_months = []
    y, m = int(first[:4]), int(first[5:7])
    while f"{y}-{m:02d}" <= through[:7]:
        all_months.append(f"{y}-{m:02d}")
        m += 1
        if m == 13:
            m, y = 1, y + 1
    if len(all_months) <= 40:
        bucket, unit = (lambda m: m), "month"
    elif len(all_months) <= 120:
        bucket, unit = (lambda m: f"{m[:4]}-Q{(int(m[5:7]) - 1) // 3 + 1}"), "quarter"
    else:
        bucket, unit = (lambda m: m[:4]), "year"
    buckets = list(dict.fromkeys(bucket(m) for m in all_months))
    per = {}
    for d in docs:
        m = (d["meeting_date"] or "")[:7]
        if len(m) == 7:
            per[bucket(m)] = per.get(bucket(m), 0) + 1

    # Axis labels: one per year, placed where the year starts.
    def axis_label(i, b):
        year_start = (unit == "year") or b.endswith(("-01", "-Q1"))
        if unit == "year":
            return b if (len(buckets) <= 12 or i % 2 == 0) else ""
        if year_start:
            return b[:4]
        if i == 0:
            # Label the first bar only if the next year label is far enough away.
            nxt = next((j for j, x in enumerate(buckets) if x.endswith(("-01", "-Q1"))), len(buckets))
            if nxt >= (4 if unit == "month" else 3):
                return apmonth(b) if unit == "month" else f"{b[5:]} {b[:4]}"
        return ""

    timeline = [{"key": b, "n": per.get(b, 0), "axis": axis_label(i, b)}
                for i, b in enumerate(buckets)]

    # Committees where this name shows up most
    by_committee = {}
    for d in docs:
        by_committee[d["committee"] or "Other"] = by_committee.get(d["committee"] or "Other", 0) + 1
    top_committees = sorted(by_committee.items(), key=lambda kv: -kv[1])[:5]

    return render_template("entity.html", name=name, docs=docs, entity_type=entity_type,
                           timeline=timeline, timeline_unit=unit,
                           top_committees=top_committees)


@app.route("/entities/block", methods=["POST"])
def entity_block():
    if not SERVE_LOCAL:
        return "Blocklist management is only available when running locally.", 403
    name  = request.form.get("name", "").strip()
    etype = request.form.get("type", "").strip()
    if name and etype:
        db = get_db()
        db.execute("INSERT OR IGNORE INTO entity_blocklist (entity_text, entity_type) VALUES (?,?)",
                   (name, etype))
        db.commit()
    return redirect(request.referrer or url_for("entities"))


@app.route("/entities/unblock", methods=["POST"])
def entity_unblock():
    if not SERVE_LOCAL:
        return "Blocklist management is only available when running locally.", 403
    name  = request.form.get("name", "").strip()
    etype = request.form.get("type", "").strip()
    if name and etype:
        db = get_db()
        db.execute("DELETE FROM entity_blocklist WHERE entity_text=? AND entity_type=?", (name, etype))
        db.commit()
    return redirect(url_for("entity_blocked"))


@app.route("/entities/blocked")
def entity_blocked():
    db   = get_db()
    rows = db.execute(
        "SELECT entity_text, entity_type FROM entity_blocklist ORDER BY entity_type, entity_text"
    ).fetchall()
    return render_template("blocked.html", blocked=rows, admin_mode=SERVE_LOCAL)


@app.route("/serve/<path:filename>")
def serve_doc(filename):
    if not SERVE_LOCAL:
        db = get_db()
        row = db.execute("SELECT doc_url FROM documents WHERE filename = ?", (filename,)).fetchone()
        if row:
            return redirect(row["doc_url"], 302)
        return "File not available", 404
    return send_from_directory("docs", filename)


@app.route("/render/<int:doc_id>")
def render_doc(doc_id):
    if not SERVE_LOCAL:
        return "Document rendering is not available in this deployment.", 404
    import mammoth
    from pathlib import Path
    db  = get_db()
    d   = db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not d or d["url_ext"] not in ("docx", "doc"):
        return "Not renderable", 404
    path = Path("docs") / d["filename"]
    if not path.exists():
        return "File not found", 404
    with open(path, "rb") as f:
        result = mammoth.convert_to_html(f)
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
  body {{ font-family: Georgia, serif; max-width: 860px; margin: 2rem auto;
          padding: 0 1.5rem; line-height: 1.7; color: #1e293b; font-size: 15px; }}
  h1,h2,h3 {{ font-family: system-ui, sans-serif; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  td, th {{ border: 1px solid #cbd5e1; padding: .4rem .75rem; text-align: left; font-size: 14px; }}
  th {{ background: #f1f5f9; font-weight: 600; }}
  p {{ margin-bottom: .75rem; }}
</style></head><body>{result.value}</body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
