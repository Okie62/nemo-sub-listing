"""
NEMO submersible listing — enquiry intake API.

Accepts enquiries from the static listing at https://okie62.github.io/nemo-sub-listing/,
stores them in Postgres, and emails a distribution list.

Storage is an isolated table (`nemo_enquiries`) so it can be lifted to its own
database at any time without touching anything else.
"""

import os
import re
import smtplib
import ssl
import time
import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from functools import wraps

import psycopg
from flask import Flask, request, jsonify, Response, make_response

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nemo")

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER)
# Comma-separated distribution list
NOTIFY_TO = [a.strip() for a in os.environ.get("NOTIFY_TO", "").split(",") if a.strip()]
ADMIN_USER = os.environ.get("ADMIN_USER", "seakeepers")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "ALLOWED_ORIGINS",
        "https://okie62.github.io,http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if o.strip()
]

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

FIELDS = ("name", "email", "phone", "country", "role", "timeframe", "message")
MAXLEN = {
    "name": 120,
    "email": 200,
    "phone": 60,
    "country": 80,
    "role": 60,
    "timeframe": 60,
    "message": 4000,
}

DDL = """
CREATE TABLE IF NOT EXISTS nemo_enquiries (
    id           bigserial PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    name         text NOT NULL,
    email        text NOT NULL,
    phone        text,
    country      text,
    role         text,
    timeframe    text,
    message      text,
    source       text,
    user_agent   text,
    ip           text,
    emailed      boolean NOT NULL DEFAULT false,
    email_error  text
);
CREATE INDEX IF NOT EXISTS nemo_enquiries_created_idx ON nemo_enquiries (created_at DESC);
"""

_schema_ready = False


def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(DATABASE_URL, connect_timeout=10)


def ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    with db() as conn, conn.cursor() as cur:
        cur.execute(DDL)
        conn.commit()
    _schema_ready = True


def cors(resp):
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


@app.after_request
def _after(resp):
    return cors(resp)


@app.get("/health")
def health():
    return jsonify(ok=True, ts=datetime.now(timezone.utc).isoformat())


@app.route("/enquiry", methods=["POST", "OPTIONS"])
def enquiry():
    if request.method == "OPTIONS":
        return make_response("", 204)

    data = request.get_json(silent=True) or request.form or {}

    # Honeypot: real users never fill this.
    if (data.get("website") or "").strip():
        log.info("honeypot hit, silently accepting")
        return jsonify(ok=True), 200

    vals = {}
    for f in FIELDS:
        v = (data.get(f) or "").strip()
        vals[f] = v[: MAXLEN[f]]

    errors = {}
    if not vals["name"]:
        errors["name"] = "Please enter your name."
    if not vals["email"]:
        errors["email"] = "Please enter your email address."
    elif not EMAIL_RE.match(vals["email"]):
        errors["email"] = "That email address does not look valid."
    if not vals["message"]:
        errors["message"] = "Please tell us briefly what you would like to know."
    if errors:
        return jsonify(ok=False, errors=errors), 400

    ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "").split(",")[0].strip()
    ua = request.headers.get("User-Agent", "")[:400]
    source = (data.get("source") or request.headers.get("Referer") or "")[:300]

    try:
        ensure_schema()
        with db() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO nemo_enquiries
                   (name, email, phone, country, role, timeframe, message, source, user_agent, ip)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id, created_at""",
                (
                    vals["name"], vals["email"], vals["phone"], vals["country"],
                    vals["role"], vals["timeframe"], vals["message"], source, ua, ip,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        enq_id, created = row[0], row[1]
    except Exception as e:
        log.exception("db insert failed")
        return jsonify(ok=False, error="Could not record the enquiry. Please email southpacific@seakeepers.org."), 500

    err = None
    try:
        send_notification(enq_id, created, vals, ip)
    except Exception as e:  # storage already succeeded — never fail the user for this
        err = f"{type(e).__name__}: {e}"[:500]
        log.exception("notification email failed")

    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE nemo_enquiries SET emailed=%s, email_error=%s WHERE id=%s",
                (err is None, err, enq_id),
            )
            conn.commit()
    except Exception:
        log.exception("could not record email status")

    return jsonify(ok=True, id=enq_id), 200


def send_notification(enq_id, created, v, ip):
    if not (SMTP_USER and SMTP_PASS and NOTIFY_TO):
        raise RuntimeError("SMTP or NOTIFY_TO not configured")

    subject = f"NEMO enquiry #{enq_id} — {v['name']}" + (f" ({v['country']})" if v["country"] else "")
    lines = [
        "New enquiry from the U-Boat Worx NEMO NM2-100 listing.",
        "",
        f"Received : {created:%d %b %Y %H:%M UTC}",
        f"Name     : {v['name']}",
        f"Email    : {v['email']}",
        f"Phone    : {v['phone'] or '—'}",
        f"Country  : {v['country'] or '—'}",
        f"Enquirer : {v['role'] or '—'}",
        f"Timeframe: {v['timeframe'] or '—'}",
        "",
        "Message",
        "-------",
        v["message"],
        "",
        f"(enquiry #{enq_id} · ip {ip or 'unknown'})",
        "https://okie62.github.io/nemo-sub-listing/",
    ]
    body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(NOTIFY_TO)
    msg["Reply-To"] = f"{v['name']} <{v['email']}>"
    msg.set_content(body)

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=25) as s:
        s.starttls(context=ctx)
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    log.info("notified %s for enquiry %s", NOTIFY_TO, enq_id)


def require_admin(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        auth = request.authorization
        if not ADMIN_PASS or not auth or auth.username != ADMIN_USER or auth.password != ADMIN_PASS:
            return Response(
                "Authentication required.", 401,
                {"WWW-Authenticate": 'Basic realm="NEMO enquiries"'},
            )
        return fn(*a, **kw)
    return wrapper


@app.get("/admin")
@require_admin
def admin():
    ensure_schema()
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id, created_at, name, email, phone, country, role, timeframe,
                      message, emailed, email_error
               FROM nemo_enquiries ORDER BY created_at DESC LIMIT 500"""
        )
        rows = cur.fetchall()

    def esc(s):
        s = "" if s is None else str(s)
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    trs = []
    for r in rows:
        (i, ts, name, email, phone, country, role, tf, msg, emailed, eerr) = r
        flag = "✓" if emailed else f'<span title="{esc(eerr)}">✗</span>'
        trs.append(
            f"<tr><td>{i}</td><td>{ts:%Y-%m-%d %H:%M}</td><td>{esc(name)}</td>"
            f"<td><a href='mailto:{esc(email)}'>{esc(email)}</a></td><td>{esc(phone)}</td>"
            f"<td>{esc(country)}</td><td>{esc(role)}</td><td>{esc(tf)}</td>"
            f"<td class='m'>{esc(msg)}</td><td style='text-align:center'>{flag}</td></tr>"
        )

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>NEMO enquiries</title><meta name="robots" content="noindex">
<style>
body{{font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0b1620;color:#e8eef3;margin:0;padding:28px}}
h1{{font-weight:600;font-size:20px;margin:0 0 4px}} p.sub{{color:#8aa0b0;margin:0 0 22px}}
table{{border-collapse:collapse;width:100%;background:#101f2b}}
th,td{{padding:9px 11px;border-bottom:1px solid #1e3243;vertical-align:top;text-align:left}}
th{{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:#7f98a9;background:#0d1a24;position:sticky;top:0}}
td.m{{max-width:420px;white-space:pre-wrap}} a{{color:#59c6e6}}
</style></head><body>
<h1>NEMO NM2-100 — enquiries</h1>
<p class="sub">{len(rows)} record(s). Newest first. Last column: distribution email sent.</p>
<table><tr><th>#</th><th>Received (UTC)</th><th>Name</th><th>Email</th><th>Phone</th>
<th>Country</th><th>Enquirer</th><th>Timeframe</th><th>Message</th><th>Mail</th></tr>
{''.join(trs) or '<tr><td colspan=10>No enquiries yet.</td></tr>'}
</table></body></html>"""
    return Response(html, mimetype="text/html")


@app.get("/")
def root():
    return jsonify(service="nemo-enquiries", ok=True)


if __name__ == "__main__":
    app.run(port=8099, debug=True)
