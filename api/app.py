"""
NEMO submersible listing — enquiry intake API.

Accepts enquiries from the static listing at https://okie62.github.io/nemo-sub-listing/,
stores them in Postgres, and emails a distribution list.

Storage is an isolated table (`nemo_enquiries`) so it can be lifted to its own
database at any time without touching anything else.
"""

import os
import re
import base64
import json
import smtplib
import socket
import ssl
import urllib.request
import urllib.parse
import urllib.error
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
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "NEMO Listing Enquiries")
SENDGRID_KEY = os.environ.get("SENDGRID_API_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
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


def _sendgrid_send(msg):
    """
    Preferred transport. HTTPS/443, and oktechsol.com's SPF record already
    includes sendgrid.net, so mail genuinely authenticates as
    jay@oktechsol.com rather than being rewritten by the provider.
    """
    if not SENDGRID_KEY:
        raise RuntimeError("SENDGRID_API_KEY not configured")

    payload = {
        "personalizations": [{"to": [{"email": a} for a in NOTIFY_TO]}],
        "from": {"email": MAIL_FROM, "name": MAIL_FROM_NAME},
        "subject": msg["Subject"],
        "content": [{"type": "text/plain", "value": msg.get_content()}],
    }
    if msg["Reply-To"]:
        addr = msg["Reply-To"]
        if "<" in addr:
            name, email = addr.rsplit("<", 1)
            payload["reply_to"] = {"email": email.strip(" >"), "name": name.strip()}
        else:
            payload["reply_to"] = {"email": addr.strip()}

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Authorization": "Bearer " + SENDGRID_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status not in (200, 202):
            raise RuntimeError(f"sendgrid returned {resp.status}")
    log.info("sendgrid accepted mail for %s", NOTIFY_TO)
    return "sendgrid"


def _gmail_send(msg):
    """
    Primary transport. Render's free instances block outbound SMTP (25/465/587) —
    the TCP connect hangs until the worker is killed. The Gmail REST API runs
    over 443, which is open, so it works on the free plan.
    """
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN):
        raise RuntimeError("Gmail API credentials not configured")

    body = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": GOOGLE_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body, method="POST")
    with urllib.request.urlopen(req, timeout=25) as resp:
        access = json.load(resp)["access_token"]

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    req = urllib.request.Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        data=json.dumps({"raw": raw}).encode(),
        method="POST",
        headers={"Authorization": "Bearer " + access, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.load(resp)
    log.info("gmail api sent id=%s", out.get("id"))
    return out.get("id")


def _smtp_connect():
    """
    Render's container resolves smtp.gmail.com to an AAAA record first and has no
    IPv6 route out, producing 'Network is unreachable'. So resolve A (IPv4)
    records explicitly and dial those, while still validating the TLS
    certificate against the real hostname.

    Order: each IPv4 address on 587 (STARTTLS) then 465 (implicit TLS),
    finally the plain hostname as a last resort.
    """
    ctx = ssl.create_default_context()

    ipv4 = []
    try:
        ipv4 = sorted(
            {ai[4][0] for ai in socket.getaddrinfo(SMTP_HOST, None, socket.AF_INET, socket.SOCK_STREAM)}
        )
    except Exception as e:
        log.warning("IPv4 resolve of %s failed: %s", SMTP_HOST, e)

    attempts = [(ip, p) for ip in ipv4 for p in (SMTP_PORT, 465)]
    attempts += [(SMTP_HOST, SMTP_PORT), (SMTP_HOST, 465)]

    last = None
    for host, port in attempts:
        s = None
        try:
            if port == 465:
                raw = socket.create_connection((str(host), port), timeout=25)
                sock = ctx.wrap_socket(raw, server_hostname=SMTP_HOST)
                s = smtplib.SMTP(timeout=25)
                s.sock = sock
                s.file = sock.makefile("rb")
                code, _ = s.getreply()
                if code != 220:
                    raise smtplib.SMTPConnectError(code, "unexpected greeting")
                s.ehlo()
            else:
                s = smtplib.SMTP(timeout=25)
                s.connect(str(host), port)
                # cert is validated against the real hostname, not the dialled IP
                s._host = SMTP_HOST
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
            s.login(SMTP_USER, SMTP_PASS)
            log.info("smtp connected via %s:%s", host, port)
            return s
        except Exception as e:
            last = f"{host}:{port} {type(e).__name__}: {e}"
            log.warning("smtp attempt failed — %s", last)
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass
    raise RuntimeError("all SMTP attempts failed — last: " + str(last))


def _dispatch(msg, only=None):
    """
    Try each transport in order and return the name of the one that worked.
    SendGrid first (HTTPS, SPF-aligned for oktechsol.com), then Gmail API
    (HTTPS but rewrites the From to the authenticated Google account), then
    SMTP. Pass only='smtp' to force one transport when testing.
    """
    chain = (("sendgrid", _sendgrid_send), ("gmail", _gmail_send), ("smtp", _smtp_send))
    if only:
        chain = tuple(c for c in chain if c[0] == only)
        if not chain:
            raise RuntimeError(f"unknown transport '{only}'")
    errors = []
    for name, fn in chain:
        try:
            fn(msg)
            return name
        except Exception as e:
            detail = f"{name}: {type(e).__name__}: {e}"
            if isinstance(e, urllib.error.HTTPError):
                try:
                    detail += " | " + e.read().decode()[:200]
                except Exception:
                    pass
            errors.append(detail)
            log.warning("transport failed — %s", detail)
    raise RuntimeError(" ;; ".join(errors))


def _smtp_send(msg):
    # Off by default: Render free instances block outbound 25/465/587 and the
    # connect *hangs* rather than refusing, which kills the gunicorn worker
    # mid-request. Set SMTP_ENABLED=1 only on a host with SMTP egress.
    if os.environ.get("SMTP_ENABLED", "") not in ("1", "true", "yes"):
        raise RuntimeError("SMTP disabled (set SMTP_ENABLED=1 to allow)")
    if not (SMTP_USER and SMTP_PASS):
        raise RuntimeError("SMTP not configured")
    s = _smtp_connect()
    try:
        s.send_message(msg)
    finally:
        try:
            s.quit()
        except Exception:
            pass
    return "smtp"


def send_notification(enq_id, created, v, ip):
    if not NOTIFY_TO:
        raise RuntimeError("NOTIFY_TO not configured")

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
    msg["From"] = f"{MAIL_FROM_NAME} <{MAIL_FROM}>"
    msg["To"] = ", ".join(NOTIFY_TO)
    msg["Reply-To"] = f"{v['name']} <{v['email']}>"
    msg.set_content(body)

    via = _dispatch(msg)
    log.info("notified %s for enquiry %s via %s", NOTIFY_TO, enq_id, via)
    return via


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


@app.get("/admin/smtp-test")
@require_admin
def smtp_test():
    """Prove the outbound mail path works, and report exactly which transport won."""
    info = {
        "from": MAIL_FROM,
        "to": NOTIFY_TO,
        "transports_configured": {
            "sendgrid": bool(SENDGRID_KEY),
            "gmail": bool(GOOGLE_CLIENT_ID and GOOGLE_REFRESH_TOKEN),
            "smtp": bool(SMTP_USER and SMTP_PASS),
        },
    }
    try:
        msg = EmailMessage()
        msg["Subject"] = "NEMO enquiry service — delivery test"
        msg["From"] = f"{MAIL_FROM_NAME} <{MAIL_FROM}>"
        msg["To"] = ", ".join(NOTIFY_TO)
        msg.set_content(
            "This is a delivery test from the NEMO NM2-100 enquiry intake service.\n\n"
            "If you received this, enquiries submitted on the listing page will reach "
            "this distribution list.\n\n"
            "https://okie62.github.io/nemo-sub-listing/\n"
        )
        info["via"] = _dispatch(msg, only=request.args.get("via"))
        info["sent"] = True
    except Exception as e:
        info["sent"] = False
        info["error"] = f"{type(e).__name__}: {e}"[:900]
    return jsonify(info), (200 if info.get("sent") else 500)


@app.post("/admin/resend/<int:enq_id>")
@require_admin
def resend(enq_id):
    ensure_schema()
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id, created_at, name, email, phone, country, role, timeframe, message, ip
               FROM nemo_enquiries WHERE id=%s""",
            (enq_id,),
        )
        r = cur.fetchone()
    if not r:
        return jsonify(ok=False, error="not found"), 404
    v = dict(zip(("name", "email", "phone", "country", "role", "timeframe", "message"), r[2:9]))
    v = {k: (val or "") for k, val in v.items()}
    err = None
    try:
        send_notification(r[0], r[1], v, r[9])
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:500]
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE nemo_enquiries SET emailed=%s, email_error=%s WHERE id=%s", (err is None, err, enq_id))
        conn.commit()
    return jsonify(ok=err is None, error=err), (200 if err is None else 500)


@app.get("/admin/probe")
@require_admin
def probe():
    """
    TCP reachability check. Render's free tier blocks outbound SMTP ports;
    paid instances allow them. Use this to prove egress before wiring a
    mail host, e.g. /admin/probe?host=smtp.serverdata.net&port=587
    """
    host = request.args.get("host", SMTP_HOST)
    ports = request.args.get("port", "587,465,25").split(",")
    out = {"host": host, "results": {}}
    for p in ports:
        p = p.strip()
        if not p.isdigit():
            continue
        t0 = time.time()
        try:
            s = socket.create_connection((host, int(p)), timeout=12)
            banner = ""
            try:
                s.settimeout(6)
                banner = s.recv(200).decode(errors="replace").strip()
            except Exception:
                pass
            s.close()
            out["results"][p] = {
                "open": True,
                "ms": int((time.time() - t0) * 1000),
                "banner": banner[:160],
            }
        except Exception as e:
            out["results"][p] = {
                "open": False,
                "ms": int((time.time() - t0) * 1000),
                "error": f"{type(e).__name__}: {e}"[:160],
            }
    return jsonify(out)


@app.get("/")
def root():
    return jsonify(service="nemo-enquiries", ok=True)


if __name__ == "__main__":
    app.run(port=8099, debug=True)
