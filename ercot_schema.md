# ERCOT Meeting Minutes Database — Data Pipeline

```
ERCOT.com
    │
    ▼
ercot_enumerate.py  ──►  meetings.csv
                          (url, date, committee, title, status)
    │
    ▼
ercot_details.py    ──►  documents.csv
                          (meeting_url, doc_title, doc_url, type, size)
    │
    ▼
ercot_download.py   ──►  docs/
                          01142025-board__Meeting_Minutes.pdf
                          01142025-board__Agenda.pdf
                          ...
    │
    ▼
build_db.py         ──►  ercot.db
                          ├── meetings
                          ├── documents
                          └── document_text (FTS5)
    │
    ▼
Flask app           ──►  Render
                          ├── /           search box
                          ├── /search     full-text results
                          ├── /meetings   browse by committee/date
                          └── /meeting/   agenda + docs
```
