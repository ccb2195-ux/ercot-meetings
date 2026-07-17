# ERCOT Meeting Database

A searchable archive of ERCOT committee meeting documents — agendas, minutes, presentations, and supporting materials — with full-text search and named entity extraction.

**Live site:** [ercot-meetings.onrender.com](https://ercot-meetings.onrender.com)

---

## Methodology

### Data Collection

Documents are scraped directly from the [ERCOT public calendar](https://www.ercot.com/calendar). No API — the pipeline parses the calendar HTML to enumerate meetings, then fetches each meeting page to collect document links and metadata.

```
ercot_enumerate.py   →  meetings.csv       (meeting URL, date, committee, title, status)
ercot_details.py     →  documents.csv      (document URL, title, file type, size)
ercot_download.py    →  docs/              (raw files: PDF, DOCX, PPTX, XLSX, HTML)
```

### Text Extraction

Each downloaded file is parsed to plain text using format-specific libraries:

| Format | Library |
|--------|---------|
| PDF | PyMuPDF (`fitz`) |
| DOCX / DOC | python-docx |
| PPTX | python-pptx + pytesseract (slide image OCR) |
| XLSX / XLS | openpyxl, xlrd |
| HTML | BeautifulSoup4 |

```
extract_text.py   →  document_text table in ercot.db
```

### Named Entity Recognition

Document text is run through [spaCy](https://spacy.io/) (`en_core_web_lg`) to extract:
- **PERSON** — individuals named in meeting materials
- **ORG** — organizations, companies, and working groups
- **NPRR** — custom regex pattern for ERCOT nodal protocol revision requests

```
extract_entities.py   →  entities table in ercot.db
```

### Database

SQLite with [FTS5](https://www.sqlite.org/fts5.html) full-text search. The local database (~900 MB) holds full document text. A slim deployment copy (~240 MB) is built for hosting:

```
build_db.py          →  ercot.db           (full local DB with document_text)
build_public_db.py   →  ercot_public.db    (slim DB: contentless FTS index + 300-char teasers)
```

The slim DB keeps the full FTS inverted index (so search quality is identical) but drops the stored document text, cutting the file size by ~74%.

### Web Application

Flask app with SQLite/FTS5 backend. Features:
- Full-text search across all document content
- Entity browser (people, organizations, NPRR numbers)
- Homepage visualizations: top entities by mention count, 6-month trend charts (Chart.js)
- Meeting and document metadata browsing
- CSV export of document metadata

### Deployment

The app runs on [Render](https://render.com) (free tier). Because Render has no persistent storage, the database is distributed as a [GitHub Release](https://github.com/ccb2195-ux/ercot-meetings/releases) asset and downloaded at startup via `startup.py`.

[UptimeRobot](https://uptimerobot.com) pings the service every 5 minutes to prevent spin-down and avoid repeated cold-start downloads.

---

## Pipeline

### Monthly update (run on the 1st)
```bash
python monthly_update.py
```
Scrapes the current month, rebuilds the DB, builds the public DB, and prompts to publish a new GitHub Release.

### Backfill a historical year
```bash
python backfill_year.py --year 2022
```

---

## Stack

| Layer | Tool |
|-------|------|
| Scraping | Python `requests` + `re` |
| Text extraction | PyMuPDF, python-docx, python-pptx, openpyxl, BeautifulSoup4 |
| NER | spaCy `en_core_web_lg` |
| Database | SQLite 3 + FTS5 |
| Web framework | Flask + Jinja2 |
| Production server | Gunicorn |
| Hosting | Render (free tier) |
| DB distribution | GitHub Releases |
| Visualizations | Chart.js 4 |
