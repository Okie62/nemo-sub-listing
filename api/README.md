# NEMO NM2-100 listing — operations

Public listing: https://okie62.github.io/nemo-sub-listing/ (GitHub Pages, `index.html`)
Enquiry API:    https://nemo-enquiries.onrender.com (Render, `api/`, auto-deploys from `main`)

## Where enquiries go

1. Stored in Postgres, table `nemo_enquiries` on the existing `yachts-ai` database
   (isolated table — no new database, no new monthly cost).
2. Emailed to the distribution list in `NOTIFY_TO`:
   **jay@oktechsol.com, lance@sheppard.co.nz**

Storage and email are independent. If email fails the enquiry is still saved and
flagged `emailed = false` with the error, so a lead can never be lost silently.

## Admin

| URL | Purpose |
|---|---|
| `/admin` | All enquiries, newest first. Last column = email delivered. |
| `/admin/smtp-test` | Sends a test message; reports which transport delivered. |
| `/admin/resend/<id>` | POST. Re-sends the notification for one enquiry. |
| `/health` | Liveness. Also hit by the page to warm the service. |

Basic auth: `seakeepers` / `NemoSK-TNgdrHkmYdB5eQ` (env `ADMIN_USER` / `ADMIN_PASS`).

## Mail transports — read before changing

`_dispatch()` tries three transports in order and records which one won.

1. **SendGrid REST** (443) — *preferred, not yet enabled.* Set `SENDGRID_API_KEY`
   and it takes over automatically. This is the only transport that puts a true
   `From: jay@oktechsol.com` on the wire: oktechsol.com's SPF record is `-all`
   (hard fail) and already includes `sendgrid.net`.
2. **Gmail REST** (443) — *currently active.* Works, but Google rewrites the
   From to the authenticated account, so mail arrives as
   `Jay Wade <jay@redearthsystems.com>`. `Reply-To` is still the enquirer.
   jay@oktechsol.com is not a verified send-as alias on that Google account.
3. **SMTP** — disabled unless `SMTP_ENABLED=1`.

### Why SMTP is off

Render's **free** instances block outbound 25/465/587. The connect does not get
refused — it *hangs* until gunicorn kills the worker mid-request
(`SystemExit: 1` inside `socket.create_connection`). Confirmed in service logs.
Leaving SMTP in the chain on a free instance costs ~25s per attempt and can take
down the worker. Only set `SMTP_ENABLED=1` on a host with real SMTP egress.

## To get From: jay@oktechsol.com

Create a SendGrid API key with Mail Send permission, verify jay@oktechsol.com as
a sender, then:

```
SENDGRID_API_KEY=SG.xxxx    # on the Render service
```

No code change needed. Confirm with `/admin/smtp-test` — it should report
`"via": "sendgrid"`.

## Free tier caveat

The service sleeps after inactivity. The listing page pings `/health` when a
visitor scrolls within 400px of the enquiry section, so the cold start is
usually absorbed before they press Send. Upgrading to the $7/mo starter plan
removes both the sleep and the SMTP block.

## Not published

`docs/` (the logbook PDF and spec sheet) is deliberately excluded from the repo —
buyer-qualified material, verified returning 404 on the live site.
