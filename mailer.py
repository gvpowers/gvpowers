"""Enquiry email flow — sent via SMTP after a successful enquiry.

Two emails are dispatched for every submitted enquiry:

  1. Company notification — the lead is emailed to the GV POWERS inbox
     (``COMPANY_NOTIFY_EMAIL``) with the customer as ``Reply-To`` so a reply
     from the company inbox goes straight back to the customer.
  2. Customer thank-you — a branded "we received your enquiry" email with the
     GV POWERS logo, reference number and the customer's own message echoed
     back.

Delivery is synchronous on purpose. The contact route waits for both messages
so a failure is visible in the request log instead of disappearing inside a
daemon thread when a production worker is recycled. A failed email never loses
the enquiry: it is already stored in SQLite before this module runs.

Configuration comes from environment variables (see ``.env.example``):

    SMTP_HOST             default smtp.gmail.com
    SMTP_PORT             default 465
    SMTP_SECURITY         auto | ssl | starttls | plain (auto: 465=ssl,
                          587=starttls, anything else=plain)
    SMTP_TIMEOUT          socket timeout in seconds (default 20)
    SMTP_USER             mailbox used to send
    SMTP_APP_PASSWORD     app password (NOT the account password)
    MAIL_FROM             optional From: address override
    MAIL_FROM_NAME        optional From: display name
    MAIL_REPLY_TO         Reply-To for the customer thank-you
    COMPANY_NOTIFY_EMAIL  inbox that receives every enquiry lead
    SITE_URL              public website URL used in the email footer
    CATALOGUE_URL         catalogue URL used by the call-to-action button

If ``SMTP_USER`` / ``SMTP_APP_PASSWORD`` are missing, sending is skipped
gracefully: the enquiry is still stored and the request still succeeds.
"""
import html
import logging
import os
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

from site_data import COMPANY

logger = logging.getLogger("gvpowers.mailer")

SERVICE_LABELS = {
    "solar": "Solar Energy",
    "ups": "UPS Systems",
    "generators": "Generators",
    "bess": "Battery Energy Storage",
    "amc": "AMC / Service",
    "general": "General Enquiry",
}

# Editorial Luxury palette (kept in sync with static/css/style.css)
IVORY = "#F4EEE3"
PAPER_DEEP = "#EAE1D1"
PAPER_SOFT = "#FBF8F2"
BRONZE = "#8A5A2B"
BRONZE_LIGHT = "#C9A26F"
EVERGREEN = "#223029"
EVERGREEN_DEEP = "#131D17"
INK = "#221F19"
MUTED = "#5E5A4F"
LINE = "#D9CDB8"

DEFAULT_SITE_URL = "https://gvpowers.in"
DEFAULT_CATALOGUE_URL = "https://catalog.gvpowers.in"
EMAIL_LOGO_CID = "gvpowers-logo"
_EMAIL_LOGO_FILENAME = "logo-email.png"

_SECURITY_ALIASES = {
    "ssl": "ssl",
    "smtps": "ssl",
    "starttls": "starttls",
    "start-tls": "starttls",
    "tls": "starttls",
    "plain": "plain",
    "none": "plain",
    "": "auto",
    "auto": "auto",
}


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _int_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r — using default %s", name, raw, default)
        return default
    return max(low, min(high, value))


def _security_for_port(port: int) -> str:
    if port == 465:
        return "ssl"
    if port == 587:
        return "starttls"
    return "plain"


def _normalise_security(value: str, port: int) -> str:
    key = (value or "").strip().lower()
    security = _SECURITY_ALIASES.get(key)
    if security is None:
        logger.warning(
            "Unknown SMTP_SECURITY=%r — deriving it from port %s", value, port
        )
        security = "auto"
    if security == "auto":
        return _security_for_port(port)
    if port == 465 and security != "ssl":
        logger.warning(
            "SMTP_SECURITY=%s is not valid on port 465 — using implicit SSL",
            security,
        )
        return "ssl"
    return security


def _company_recipient() -> str:
    return (
        os.environ.get("COMPANY_NOTIFY_EMAIL", "").strip()
        or COMPANY["email"]
    )


def _safe_url(value: str, fallback: str) -> str:
    value = (value or "").strip()
    if value.startswith(("https://", "http://")):
        return value.rstrip("/")
    return fallback


def _smtp_settings():
    """Return the SMTP configuration, or ``None`` when mail is disabled."""
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_APP_PASSWORD", "").strip()
    if not user or not password:
        return None

    port = _int_env("SMTP_PORT", 465, 1, 65535)
    return {
        "host": os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
        or "smtp.gmail.com",
        "port": port,
        "security": _normalise_security(
            os.environ.get("SMTP_SECURITY", ""), port
        ),
        "timeout": _int_env("SMTP_TIMEOUT", 20, 5, 120),
        "user": user,
        "password": password,
        "from_addr": os.environ.get("MAIL_FROM", "").strip() or user,
        "from_name": os.environ.get("MAIL_FROM_NAME", "").strip() or "GV POWERS",
        "reply_to": (
            os.environ.get("MAIL_REPLY_TO", "").strip() or _company_recipient()
        ),
        "site_url": _safe_url(
            os.environ.get("SITE_URL", ""), DEFAULT_SITE_URL
        ),
        "catalogue_url": _safe_url(
            os.environ.get("CATALOGUE_URL", ""), DEFAULT_CATALOGUE_URL
        ),
    }


def is_configured() -> bool:
    return _smtp_settings() is not None


# ---------------------------------------------------------------------------
# Small formatting / escaping helpers
# ---------------------------------------------------------------------------

def _one_line(value, limit: int = 200) -> str:
    """Collapse whitespace and strip CR/LF so headers cannot be injected."""
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())[:limit]


def _e(value, limit: int = 4000) -> str:
    """Escape user text for safe inclusion in an HTML email."""
    return html.escape(str(value or "")[:limit], quote=True)


def _address(name, email_addr) -> str:
    email_addr = _one_line(email_addr, 254)
    if not email_addr:
        return ""
    name = _one_line(name, 120)
    if not name:
        return email_addr
    return formataddr((name, email_addr))


def _reference(enquiry: dict) -> str:
    reference = _one_line(enquiry.get("reference"), 40)
    if reference:
        return reference
    for key in ("enquiry_id", "id"):
        value = enquiry.get(key)
        if value is None:
            continue
        try:
            return f"GV-{int(value):06d}"
        except (TypeError, ValueError):
            pass
    return "GV-ENQUIRY"


def _service_label(enquiry: dict) -> str:
    key = str(enquiry.get("service", "")).strip().lower()
    return SERVICE_LABELS.get(key, "General Enquiry")


def _first_name(name: str) -> str:
    name = _one_line(name, 120)
    return name.split(" ", 1)[0] if name else "there"


def _now_ist() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:  # tzdata may be unavailable on a minimal Windows host
        return datetime.now(timezone(timedelta(hours=5, minutes=30)))


def _stamp(value: datetime | None = None) -> str:
    value = value or _now_ist()
    return value.strftime("%d %b %Y, %I:%M %p IST")


def _date_short(value: datetime | None = None) -> str:
    value = value or _now_ist()
    return value.strftime("%d %b %Y")


def _tel_href(phone) -> str:
    digits = re.sub(r"[^0-9+]", "", str(phone or ""))
    return digits[:20]


def _logo_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "static", "images", "branding", _EMAIL_LOGO_FILENAME)


def _logo_available() -> bool:
    return os.path.isfile(_logo_path())


def _attach_logo(msg) -> bool:
    path = _logo_path()
    if not os.path.isfile(path):
        logger.warning("Email logo missing at %s — sending without it", path)
        return False
    with open(path, "rb") as handle:
        image = MIMEImage(handle.read(), _subtype="png")
    image.add_header("Content-ID", f"<{EMAIL_LOGO_CID}>")
    image.add_header(
        "Content-Disposition", "inline", filename="gvpowers-logo.png"
    )
    msg.attach(image)
    return True


def _apply_headers(msg, smtp: dict, subject: str, to_header: str,
                   reply_to: str | None = None) -> None:
    from_addr = _one_line(smtp.get("from_addr"), 254)
    msg["Subject"] = _one_line(subject, 220)
    msg["From"] = _address(smtp.get("from_name"), from_addr)
    msg["To"] = to_header
    # `reply_to` is already a formatted address (possibly with a display name),
    # so only strip CR/LF here instead of wrapping it a second time.
    reply_to = str(reply_to or "").replace("\r", " ").replace("\n", " ").strip()
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Date"] = formatdate(localtime=False)
    domain = from_addr.rpartition("@")[2] or "gvpowers.in"
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["X-Mailer"] = "GV POWERS Website"


def _button(label: str, href: str, background: str = EVERGREEN,
            colour: str = IVORY) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td bgcolor="{background}" style="background-color:{background};'
        f'padding:14px 24px;mso-padding-alt:14px 24px;">'
        f'<a href="{_e(href)}" style="display:inline-block;font-family:Arial,'
        f"Helvetica,sans-serif;font-size:11px;line-height:1.2;font-weight:700;"
        f"letter-spacing:.14em;text-transform:uppercase;color:{colour};"
        f'text-decoration:none;">{_e(label)}</a>'
        "</td></tr></table>"
    )


def _detail_row(label: str, value_html: str, last: bool = False) -> str:
    border = "" if last else f"border-bottom:1px solid {LINE};"
    return (
        "<tr>"
        f'<td width="30%" valign="top" style="padding:12px 14px 12px 0;'
        f'{border}font-family:Arial,Helvetica,sans-serif;font-size:11px;'
        f'letter-spacing:.10em;text-transform:uppercase;color:{MUTED};">'
        f"{_e(label)}</td>"
        f'<td valign="top" style="padding:12px 0;{border}font-family:Arial,'
        f'Helvetica,sans-serif;font-size:14px;line-height:1.6;color:{INK};">'
        f"{value_html}</td>"
        "</tr>"
    )


def _email_shell(smtp: dict, title: str, preheader: str,
                 body_html: str, legal_note: str) -> str:
    """Wrap message rows in the shared Editorial Luxury email shell."""
    site_url = _safe_url(smtp.get("site_url"), DEFAULT_SITE_URL)
    logo = (
        f'<img src="cid:{EMAIL_LOGO_CID}" width="220" alt="GV POWERS" '
        'style="display:block;width:220px;max-width:72%;height:auto;'
        'border:0;outline:none;text-decoration:none;">'
        if _logo_available()
        else (
            f'<span style="font-family:Georgia,\'Times New Roman\',serif;'
            f'font-size:26px;letter-spacing:.10em;color:{EVERGREEN};">'
            "GV POWERS</span>"
        )
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{_e(title)}</title>
</head>
<body style="margin:0;padding:0;width:100%;background-color:{PAPER_DEEP};-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;">
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;font-size:1px;line-height:1px;color:{PAPER_DEEP};opacity:0;">{_e(preheader)}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{PAPER_DEEP};">
  <tr>
    <td align="center" style="padding:28px 12px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;background-color:{IVORY};border:1px solid {LINE};">
        <tr>
          <td style="height:4px;background-color:{BRONZE};font-size:0;line-height:0;">&nbsp;</td>
        </tr>
        <tr>
          <td align="center" style="padding:30px 36px 20px 36px;background-color:{IVORY};">{logo}</td>
        </tr>
        <tr>
          <td style="padding:0 36px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr><td style="border-top:1px solid {BRONZE_LIGHT};font-size:0;line-height:0;">&nbsp;</td></tr>
            </table>
          </td>
        </tr>
{body_html}
        <tr>
          <td style="padding:28px 36px;background-color:{EVERGREEN};">
            <p style="margin:0;font-family:Georgia,'Times New Roman',serif;font-size:18px;letter-spacing:.06em;color:{IVORY};">GV POWERS</p>
            <p style="margin:5px 0 0;font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:1.5;letter-spacing:.18em;text-transform:uppercase;color:{BRONZE_LIGHT};">Powering A Better Tomorrow &middot; Est. 1997</p>
            <p style="margin:16px 0 0;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.8;color:#C3CEC6;">
              Chennai &amp; Salem, Tamil Nadu<br>
              <a href="tel:+91{_e(COMPANY['phone_primary'])}" style="color:{IVORY};text-decoration:none;">{_e(COMPANY['phone_primary_display'])}</a>
              &nbsp;&middot;&nbsp;
              <a href="tel:+91{_e(COMPANY['phone_secondary'])}" style="color:{IVORY};text-decoration:none;">{_e(COMPANY['phone_secondary_display'])}</a><br>
              <a href="mailto:{_e(COMPANY['email'])}" style="color:{BRONZE_LIGHT};text-decoration:none;">{_e(COMPANY['email'])}</a>
              &nbsp;&middot;&nbsp;
              <a href="{_e(site_url)}" style="color:{BRONZE_LIGHT};text-decoration:none;">gvpowers.in</a>
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 36px;background-color:{EVERGREEN_DEEP};">
            <p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:1.7;color:#9AA9A0;">{_e(legal_note)}</p>
            <p style="margin:10px 0 0;font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:1.7;color:#7F9187;">Developed by <a href="https://kamleshvar.online" style="color:{BRONZE_LIGHT};text-decoration:none;">GV Kamleshvar</a> &middot; <a href="https://techorigin.online" style="color:{BRONZE_LIGHT};text-decoration:none;">TechOrigin</a></p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Customer thank-you email
# ---------------------------------------------------------------------------

def _customer_plain(to_name: str, enquiry: dict, smtp: dict) -> str:
    name = _one_line(to_name or enquiry.get("name"), 120)
    greeting = f"Dear {name}," if name else "Hello,"
    reference = _reference(enquiry)
    service = _service_label(enquiry)
    received = _stamp()
    message = str(enquiry.get("message", "") or "")
    catalogue = _safe_url(smtp.get("catalogue_url"), DEFAULT_CATALOGUE_URL)
    site = _safe_url(smtp.get("site_url"), DEFAULT_SITE_URL)
    return f"""GV POWERS
Powering A Better Tomorrow

{greeting}

Thank you for contacting GV POWERS. We have received your enquiry and our
team will get back to you shortly.

Reference: {reference}
Service:   {service}
Received:  {received}

Your message:
{message}

Need to talk sooner?
  Call:      {COMPANY['phone_primary_display']}  |  {COMPANY['phone_secondary_display']}
  WhatsApp:  https://wa.me/{COMPANY['whatsapp']}
  Email:     {COMPANY['email']}
  Hours:     {COMPANY['hours']}, all days
  Catalogue: {catalogue}
  Website:   {site}

Chennai & Salem, Tamil Nadu.

Warm regards,
Team GV POWERS
Established 1997

---
You are receiving this email because you submitted an enquiry at {site}.
Developed by GV Kamleshvar (kamleshvar.online) · TechOrigin (techorigin.online)
"""


def _customer_html(to_name: str, enquiry: dict, smtp: dict) -> str:
    name = _one_line(to_name or enquiry.get("name"), 120)
    first = _first_name(name)
    greeting = (
        f"Thank you, {_e(first)}."
        if name
        else "Thank you for reaching out."
    )
    reference = _reference(enquiry)
    service = _service_label(enquiry)
    received = _date_short()
    message = _e(enquiry.get("message", "")).replace("\n", "<br>")
    catalogue = _safe_url(smtp.get("catalogue_url"), DEFAULT_CATALOGUE_URL)
    whatsapp = f"https://wa.me/{COMPANY['whatsapp']}"
    phone_one = _tel_href(COMPANY["phone_primary"])
    phone_two = _tel_href(COMPANY["phone_secondary"])

    body = f"""        <tr>
          <td style="padding:32px 36px 0 36px;">
            <p style="margin:0 0 12px 0;font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:1.4;letter-spacing:.20em;text-transform:uppercase;color:{BRONZE};font-weight:700;">Enquiry received</p>
            <h1 style="margin:0;font-family:Georgia,'Times New Roman',serif;font-size:30px;line-height:1.25;font-weight:400;color:{INK};">{greeting}</h1>
            <p style="margin:16px 0 0 0;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.75;color:{MUTED};">Your enquiry has reached our team. A GV POWERS specialist will review your requirement and get back to you shortly.</p>
          </td>
        </tr>
        <tr>
          <td style="padding:26px 36px 0 36px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{PAPER_DEEP}" style="background-color:{PAPER_DEEP};border-left:3px solid {BRONZE};">
              <tr>
                <td width="40%" valign="top" style="padding:16px 14px 16px 18px;">
                  <p style="margin:0 0 4px 0;font-family:Arial,Helvetica,sans-serif;font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:{BRONZE};font-weight:700;">Reference</p>
                  <p style="margin:0;font-family:Georgia,'Times New Roman',serif;font-size:17px;line-height:1.3;color:{EVERGREEN};">{_e(reference)}</p>
                </td>
                <td width="36%" valign="top" style="padding:16px 14px;">
                  <p style="margin:0 0 4px 0;font-family:Arial,Helvetica,sans-serif;font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:{BRONZE};font-weight:700;">Service</p>
                  <p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.4;color:{INK};">{_e(service)}</p>
                </td>
                <td width="24%" valign="top" style="padding:16px 18px 16px 14px;">
                  <p style="margin:0 0 4px 0;font-family:Arial,Helvetica,sans-serif;font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:{BRONZE};font-weight:700;">Received</p>
                  <p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.4;color:{INK};">{_e(received)}</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:24px 36px 0 36px;">
            <p style="margin:0 0 8px 0;font-family:Arial,Helvetica,sans-serif;font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:{MUTED};font-weight:700;">Your message</p>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{PAPER_SOFT}" style="background-color:{PAPER_SOFT};border:1px solid #E1D6C3;">
              <tr><td style="padding:16px 18px;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.75;color:{INK};">{message}</td></tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:26px 36px 0 36px;">
            <p style="margin:0 0 8px 0;font-family:Arial,Helvetica,sans-serif;font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:{MUTED};font-weight:700;">Need to talk sooner?</p>
            <p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.8;color:{MUTED};">Call <a href="tel:{_e(phone_one)}" style="color:{BRONZE};font-weight:700;text-decoration:none;">{_e(COMPANY['phone_primary_display'])}</a> or <a href="tel:{_e(phone_two)}" style="color:{BRONZE};font-weight:700;text-decoration:none;">{_e(COMPANY['phone_secondary_display'])}</a>. Our team is available <strong style="color:{INK};">{_e(COMPANY['hours'])}, all days</strong>.</p>
            <p style="margin:8px 0 0 0;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.8;color:{MUTED};">Prefer WhatsApp? <a href="{_e(whatsapp)}" style="color:{BRONZE};text-decoration:none;">Message us on WhatsApp</a> &middot; <a href="mailto:{_e(COMPANY['email'])}" style="color:{BRONZE};text-decoration:none;">{_e(COMPANY['email'])}</a></p>
          </td>
        </tr>
        <tr>
          <td style="padding:26px 36px 36px 36px;">
            {_button("Explore the catalogue", catalogue)}
            <p style="margin:14px 0 0 0;font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:1.6;color:#7A7466;">Or simply reply to this email — it comes straight to our team.</p>
          </td>
        </tr>"""

    return _email_shell(
        smtp,
        title="Thank you for contacting GV POWERS",
        preheader=(
            f"Your enquiry {reference} has reached our team. "
            "We will be in touch shortly."
        ),
        body_html=body,
        legal_note=(
            "You are receiving this email because you submitted an enquiry "
            "at gvpowers.in. If this was not you, please ignore this message."
        ),
    )


def _build_message(to_name, to_email, enquiry, smtp):
    """Build the customer thank-you MIME message (kept as a public alias)."""
    msg = MIMEMultipart("related")
    _apply_headers(
        msg,
        smtp,
        subject="Thank you for contacting GV POWERS",
        to_header=_address(to_name, to_email),
        reply_to=_address("GV POWERS", smtp.get("reply_to")),
    )

    alternative = MIMEMultipart("alternative")
    alternative.attach(
        MIMEText(_customer_plain(to_name, enquiry, smtp), "plain", "utf-8")
    )
    alternative.attach(
        MIMEText(_customer_html(to_name, enquiry, smtp), "html", "utf-8")
    )
    msg.attach(alternative)
    _attach_logo(msg)
    return msg


# ---------------------------------------------------------------------------
# Company notification email
# ---------------------------------------------------------------------------

def _company_plain(enquiry: dict) -> str:
    name = _one_line(enquiry.get("name"), 120)
    email_addr = _one_line(enquiry.get("email"), 254)
    phone = _one_line(enquiry.get("phone"), 25)
    service = _service_label(enquiry)
    reference = _reference(enquiry)
    received = _stamp()
    message = str(enquiry.get("message", "") or "")
    return f"""GV POWERS — NEW WEBSITE ENQUIRY

Reference: {reference}
Received:  {received}

Name:      {name}
Email:     {email_addr}
Phone:     {phone}
Service:   {service}

Message:
{message}

Reply directly to this email to contact the customer, or call the number above.
This lead was captured by the gvpowers.in enquiry form.
"""


def _company_html(enquiry: dict, smtp: dict) -> str:
    name = _one_line(enquiry.get("name"), 120)
    email_addr = _one_line(enquiry.get("email"), 254)
    phone = _one_line(enquiry.get("phone"), 25)
    service = _service_label(enquiry)
    reference = _reference(enquiry)
    received = _stamp()
    message = _e(enquiry.get("message", "")).replace("\n", "<br>")
    first = _first_name(name)

    email_value = (
        f'<a href="mailto:{_e(email_addr)}" style="color:{BRONZE};'
        f'text-decoration:none;">{_e(email_addr)}</a>'
        if email_addr
        else "&mdash;"
    )
    phone_value = (
        f'<a href="tel:{_e(_tel_href(phone))}" style="color:{BRONZE};'
        f'text-decoration:none;">{_e(phone)}</a>'
        if phone
        else "&mdash;"
    )
    rows = "".join(
        [
            _detail_row("Name", _e(name)),
            _detail_row("Email", email_value),
            _detail_row("Phone", phone_value),
            _detail_row("Service", _e(service)),
            _detail_row("Reference", _e(reference)),
            _detail_row("Received", _e(received), last=True),
        ]
    )
    actions = []
    if email_addr:
        actions.append(
            _button(f"Reply to {first}", f"mailto:{email_addr}")
        )
    if phone:
        actions.append(
            _button("Call customer", f"tel:{_tel_href(phone)}", background=BRONZE)
        )
    action_html = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        + '<td style="font-size:0;line-height:0;">&nbsp;</td>'.join(
            f"<td>{item}</td>" for item in actions
        )
        + "</tr></table>"
        if actions
        else ""
    )

    body = f"""        <tr>
          <td style="padding:32px 36px 0 36px;">
            <p style="margin:0 0 12px 0;font-family:Arial,Helvetica,sans-serif;font-size:10px;line-height:1.4;letter-spacing:.20em;text-transform:uppercase;color:{BRONZE};font-weight:700;">New website enquiry &middot; {_e(reference)}</p>
            <h1 style="margin:0;font-family:Georgia,'Times New Roman',serif;font-size:30px;line-height:1.25;font-weight:400;color:{INK};">{_e(name) or 'New enquiry'}</h1>
            <p style="margin:14px 0 0 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.7;color:{MUTED};">{_e(service)} &middot; received {_e(received)}</p>
          </td>
        </tr>
        <tr>
          <td style="padding:22px 36px 0 36px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top:1px solid {LINE};">{rows}</table>
          </td>
        </tr>
        <tr>
          <td style="padding:24px 36px 0 36px;">
            <p style="margin:0 0 8px 0;font-family:Arial,Helvetica,sans-serif;font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:{MUTED};font-weight:700;">Message</p>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{PAPER_SOFT}" style="background-color:{PAPER_SOFT};border:1px solid #E1D6C3;border-left:3px solid {BRONZE};">
              <tr><td style="padding:16px 18px;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.75;color:{INK};">{message}</td></tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:26px 36px 36px 36px;">
            {action_html}
            <p style="margin:14px 0 0 0;font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:1.6;color:#7A7466;">Reply to this email to answer the customer directly.</p>
          </td>
        </tr>"""

    return _email_shell(
        smtp,
        title=f"New website enquiry — {reference}",
        preheader=f"{name or 'Customer'} · {service} · {phone}",
        body_html=body,
        legal_note=(
            "Automated notification from the GV POWERS website enquiry form. "
            "The customer's address is set as Reply-To."
        ),
    )


def _build_company_message(enquiry, smtp):
    msg = MIMEMultipart("related")
    _apply_headers(
        msg,
        smtp,
        subject=(
            f"New website enquiry · {_reference(enquiry)} · "
            f"{_service_label(enquiry)} · {_one_line(enquiry.get('name'), 120)}"
        ),
        to_header=_address("GV POWERS Enquiries", _company_recipient()),
        reply_to=_address(
            enquiry.get("name", "Customer"), enquiry.get("email", "")
        ),
    )

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(_company_plain(enquiry), "plain", "utf-8"))
    alternative.attach(MIMEText(_company_html(enquiry, smtp), "html", "utf-8"))
    msg.attach(alternative)
    _attach_logo(msg)
    return msg


# ---------------------------------------------------------------------------
# SMTP transport
# ---------------------------------------------------------------------------

def _connect(smtp: dict, port: int, security: str):
    timeout = smtp.get("timeout", 20)
    if security == "ssl":
        server = smtplib.SMTP_SSL(smtp["host"], port, timeout=timeout)
    else:
        server = smtplib.SMTP(smtp["host"], port, timeout=timeout)

    try:
        server.ehlo()
        if security == "starttls":
            server.starttls()
            server.ehlo()
    except Exception:
        close = getattr(server, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass
        raise
    return server


def _open_server(smtp: dict):
    """Connect using the configured port, then the other Gmail port once."""
    ports = [smtp["port"]]
    if smtp["port"] in (465, 587):
        ports.append(587 if smtp["port"] != 587 else 465)

    last_error = None
    for index, port in enumerate(ports):
        security = (
            smtp.get("security", "auto")
            if index == 0
            else _security_for_port(port)
        )
        try:
            return _connect(smtp, port, security)
        except (OSError, smtplib.SMTPException) as exc:
            last_error = exc
            logger.warning(
                "SMTP connection to %s:%s (%s) failed: %s: %s",
                smtp["host"], port, security, type(exc).__name__, exc,
            )
            if index + 1 < len(ports):
                logger.warning(
                    "Retrying SMTP on %s:%s (%s)",
                    smtp["host"], ports[index + 1],
                    _security_for_port(ports[index + 1]),
                )

    if last_error is not None:
        raise last_error
    raise ConnectionError("No SMTP connection could be established")


def _quit(server) -> None:
    try:
        server.quit()
    except Exception:
        pass


def _send_messages(smtp: dict, messages: list) -> list:
    """Send messages over one SMTP session; return per-message success flags."""
    server = _open_server(smtp)
    results = []
    try:
        server.login(smtp["user"], smtp["password"])
        for index, msg in enumerate(messages):
            try:
                server.send_message(msg)
            except Exception:
                logger.exception(
                    "SMTP rejected message %s/%s for %s",
                    index + 1, len(messages), smtp["user"],
                )
                results.extend([False] * (len(messages) - len(results)))
                break
            results.append(True)
    finally:
        _quit(server)
    return results


def _dispatch_smtp(smtp: dict, msg) -> None:
    """Send one message, raising if the server did not accept it."""
    results = _send_messages(smtp, [msg])
    if not results or not results[0]:
        raise smtplib.SMTPException(
            "The SMTP server did not accept the message"
        )


# ---------------------------------------------------------------------------
# Public send API
# ---------------------------------------------------------------------------

def send_company_notification_sync(enquiry: dict) -> bool:
    """Email the enquiry lead to the GV POWERS inbox synchronously."""
    smtp = _smtp_settings()
    if not smtp:
        logger.info("SMTP not configured — skipping company notification email")
        return False
    try:
        _dispatch_smtp(smtp, _build_company_message(enquiry, smtp))
        logger.info(
            "Company notification sent for %s (%s)",
            enquiry.get("email"), _reference(enquiry),
        )
        return True
    except Exception:
        logger.exception(
            "Failed to send company notification for %s (%s)",
            enquiry.get("email"), _reference(enquiry),
        )
        return False


def send_thank_you_sync(to_name: str, to_email: str, enquiry: dict) -> bool:
    """Send the branded thank-you email synchronously. Returns True if sent."""
    smtp = _smtp_settings()
    if not smtp:
        logger.info("SMTP not configured — skipping thank-you email to %s", to_email)
        return False
    try:
        _dispatch_smtp(smtp, _build_message(to_name, to_email, enquiry, smtp))
        logger.info(
            "Thank-you email sent to %s (%s)",
            to_email, _reference(enquiry),
        )
        return True
    except Exception:
        logger.exception(
            "Failed to send thank-you email to %s (%s)",
            to_email, _reference(enquiry),
        )
        return False


def send_test_email_sync(to_email: str, name: str = "GV POWERS Test") -> bool:
    """Send a sample customer thank-you email (used by ``flask mail-test``)."""
    smtp = _smtp_settings()
    if not smtp:
        logger.info("SMTP not configured — cannot send test email")
        return False
    enquiry = {
        "name": name,
        "email": to_email,
        "phone": COMPANY["phone_primary_display"],
        "service": "general",
        "reference": "GV-TEST",
        "message": (
            "This is a test of the GV POWERS enquiry email flow. "
            "If you can read this, the branded thank-you email is working."
        ),
    }
    try:
        _dispatch_smtp(smtp, _build_message(name, to_email, enquiry, smtp))
        logger.info("Test email sent to %s", to_email)
        return True
    except Exception:
        logger.exception("Failed to send test email to %s", to_email)
        return False


def dispatch_enquiry_emails(app, to_name: str, to_email: str,
                            enquiry: dict) -> dict:
    """Send the company notification and customer thank-you synchronously.

    Returns ``{"company": bool, "customer": bool}``. ``app`` is accepted for
    backwards compatibility with callers that passed the Flask app; delivery
    no longer depends on an application context or a background thread.
    """
    smtp = _smtp_settings()
    if not smtp:
        logger.info(
            "SMTP not configured — skipping enquiry emails for %s", to_email
        )
        return {"company": False, "customer": False}

    try:
        company_message = _build_company_message(enquiry, smtp)
        customer_message = _build_message(to_name, to_email, enquiry, smtp)
        results = _send_messages(smtp, [company_message, customer_message])
        outcome = {
            "company": bool(results[0]) if len(results) > 0 else False,
            "customer": bool(results[1]) if len(results) > 1 else False,
        }
        if outcome["company"] and outcome["customer"]:
            logger.info(
                "Enquiry %s delivered to company and customer (%s)",
                _reference(enquiry), to_email,
            )
        else:
            logger.warning(
                "Enquiry %s delivery incomplete (company=%s, customer=%s) for %s",
                _reference(enquiry), outcome["company"], outcome["customer"],
                to_email,
            )
        return outcome
    except Exception:
        logger.exception(
            "Failed to dispatch enquiry emails for %s (%s)",
            to_email, _reference(enquiry),
        )
        return {"company": False, "customer": False}


# Friendly aliases kept for existing imports/callers.
send_enquiry_emails = dispatch_enquiry_emails
send_test_email = send_test_email_sync
