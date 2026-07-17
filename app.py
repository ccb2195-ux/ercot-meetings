import csv
import io
import json
import os
import sqlite3
import zipfile
from datetime import date

from flask import Flask, Response, g, redirect, render_template, request, send_from_directory, url_for

app = Flask(__name__)
DB_PATH = os.environ.get("DB_PATH", "ercot.db")
SERVE_LOCAL = (
    os.environ.get("SERVE_LOCAL_DOCS", "true").lower() == "true"
    and os.path.isdir("docs")
)


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
           ORDER BY m.date DESC
           LIMIT 10"""
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

        # Build the last-6-months label list
        today = date.today()
        months = []
        for i in range(5, -1, -1):
            m, y = today.month - i, today.year
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

    return render_template("index.html", recent=recent, stats=stats,
                           type_counts=type_counts, top_committees=top_committees,
                           committees=committees, years=years,
                           top_people=top_people, top_orgs=top_orgs,
                           org_trend=org_trend, people_trend=people_trend,
                           has_entities=has_entities)


def fts_available(db):
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='document_fts'"
    ).fetchone()
    if not row:
        return False
    return db.execute("SELECT COUNT(*) FROM document_fts").fetchone()[0] > 0


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
            results = db.execute(
                f"""SELECT d.*,
                           m.title AS meeting_title,
                           {snippet_expr} AS snippet
                    FROM document_fts
                    {text_join}
                    JOIN documents d ON d.id = document_fts.rowid
                    JOIN meetings m  ON m.url = d.meeting_url
                    WHERE document_fts MATCH ?
                    {extra_where}
                    ORDER BY rank
                    LIMIT 200""",
                [q] + extra_params,
            ).fetchall()
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

    return render_template("meetings.html", meetings=rows, committees=committees,
                           years=years, selected=committee, selected_year=year)


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
           FROM entities WHERE document_id = ?
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

    return render_template("entity.html", name=name, docs=docs, entity_type=entity_type)


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
