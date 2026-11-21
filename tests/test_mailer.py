"""Tests for the enquiry email module (mailer.py)."""
import smtplib
from email.message import Message

import pytest

import mailer
from mailer import (
    SERVICE_LABELS,
    _build_company_message,
    _build_message,
    _smtp_settings,
    dispatch_enquiry_emails,
    is_configured,
    send_test_email_sync,
    send_thank_you_sync,
)


@pytest.fixture()
def smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_SECURITY", "ssl")
    monkeypatch.setenv("SMTP_TIMEOUT", "10")
    monkeypatch.setenv("SMTP_USER", "test@gvpowers.in")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "secret-password")
    monkeypatch.setenv("MAIL_FROM", "no-reply@gvpowers.in")
    monkeypatch.setenv("MAIL_FROM_NAME", "GV POWERS")
    monkeypatch.setenv("MAIL_REPLY_TO", "hello@gvpowers.in")
    monkeypatch.setenv("COMPANY_NOTIFY_EMAIL", "leads@gvpowers.in")
    monkeypatch.setenv("SITE_URL", "https://gvpowers.in")
    monkeypatch.setenv("CATALOGUE_URL", "https://catalog.gvpowers.in")
    _FakeSMTP.all_sent = []
    _FakeSMTP.last = None
    _FakeSMTP.starttls_called = False
    return _smtp_settings()


ENQUIRY = {
    "name": "Ravi Kumar",
    "email": "ravi@example.com",
    "phone": "+91 99887 76655",
    "service": "solar",
    "reference": "GV-000123",
    "message": "Need a 5 kW rooftop system.",
}


class _FakeSMTP:
    """Captures login/send calls; records the newest instance for assertions."""

    last = None
    all_sent = []  # messages across all SMTP sessions
    port = None
    starttls_called = False

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.timeout = timeout
        self.logged_in = None
        self.sent = []
        self.ehlo_calls = 0
        _FakeSMTP.last = self

    def ehlo(self):
        self.ehlo_calls += 1
        return 250, b"ok"

    def starttls(self):
        _FakeSMTP.starttls_called = True
        return 220, b"ready"

    def login(self, user, password):
        self.logged_in = (user, password)
        return 235, b"accepted"

    def send_message(self, msg):
        self.sent.append(msg)
        _FakeSMTP.all_sent.append(msg)
        return {}

    def quit(self):
        pass

    def close(self):
        pass


def _extract(payload, content_type):
    """Walk a MIME message and return the decoded body for a content type."""
    stack = [payload]
    while stack:
        part = stack.pop()
        if part.get_content_type() == content_type:
            return part.get_payload(decode=True).decode("utf-8")
        if part.is_multipart():
            stack.extend(part.get_payload())
    return ""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_not_configured_when_creds_missing(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "")
    assert is_configured() is False


def test_configured_with_creds(smtp_env):
    assert smtp_env["user"] == "test@gvpowers.in"
    assert smtp_env["security"] == "ssl"
    assert smtp_env["timeout"] == 10
    assert is_configured() is True


def test_defaults_host_and_port(monkeypatch):
    for var in (
        "SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_TIMEOUT",
        "MAIL_FROM", "MAIL_FROM_NAME", "MAIL_REPLY_TO",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SMTP_USER", "u@x.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "p")
    s = _smtp_settings()
    assert s["host"] == "smtp.gmail.com"
    assert s["port"] == 465
    assert s["security"] == "ssl"
    assert s["timeout"] == 20
    assert s["from_addr"] == "u@x.com"
    assert s["from_name"] == "GV POWERS"
    assert s["reply_to"] == mailer._company_recipient()


def test_security_derived_from_port(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "u@x.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "p")
    monkeypatch.delenv("SMTP_SECURITY", raising=False)

    monkeypatch.setenv("SMTP_PORT", "587")
    assert _smtp_settings()["security"] == "starttls"

    monkeypatch.setenv("SMTP_PORT", "2525")
    assert _smtp_settings()["security"] == "plain"


def test_explicit_tls_alias_is_normalised(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "u@x.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "p")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_SECURITY", "tls")
    assert _smtp_settings()["security"] == "starttls"


# ---------------------------------------------------------------------------
# Message building
# ---------------------------------------------------------------------------

def test_build_message_headers(smtp_env):
    msg = _build_message("Ravi Kumar", "ravi@example.com", ENQUIRY, smtp_env)
    assert msg["Subject"] == "Thank you for contacting GV POWERS"
    assert "GV POWERS <no-reply@gvpowers.in>" == msg["From"]
    assert "Ravi Kumar <ravi@example.com>" == msg["To"]
    assert "hello@gvpowers.in" in msg["Reply-To"]
    assert msg["Date"]
    assert msg["Message-ID"].startswith("<")
    assert "@" in msg["Message-ID"]


def test_build_message_has_transparent_logo(smtp_env):
    msg = _build_message("Ravi Kumar", "ravi@example.com", ENQUIRY, smtp_env)
    images = [p for p in msg.walk() if p.get_content_type() == "image/png"]
    assert images, "logo PNG must be attached"
    assert images[0]["Content-ID"] == "<gvpowers-logo>"
    assert "inline" in images[0]["Content-Disposition"]
    assert len(images[0].get_payload(decode=True)) > 1000


def test_html_contains_details_reference_and_branding(smtp_env):
    msg = _build_message("Ravi Kumar", "ravi@example.com", ENQUIRY, smtp_env)
    html = _extract(msg, "text/html")
    assert "Ravi" in html
    assert "Thank you, Ravi." in html
    assert SERVICE_LABELS["solar"] in html
    assert "Need a 5 kW rooftop system." in html
    assert "GV-000123" in html
    assert "#223029" in html
    assert "#8A5A2B" in html
    assert "#F4EEE3" in html
    assert "kamleshvar.online" in html
    assert "techorigin.online" in html
    assert "cid:gvpowers-logo" in html


def test_plain_text_contains_details(smtp_env):
    msg = _build_message("Ravi Kumar", "ravi@example.com", ENQUIRY, smtp_env)
    plain = _extract(msg, "text/plain")
    assert "Thank you for contacting GV POWERS" in plain
    assert "GV-000123" in plain
    assert SERVICE_LABELS["solar"] in plain
    assert "Need a 5 kW rooftop system." in plain


def test_unknown_service_falls_back(smtp_env):
    enquiry = dict(ENQUIRY, service="mystery")
    msg = _build_message("Ravi Kumar", "ravi@example.com", enquiry, smtp_env)
    html = _extract(msg, "text/html")
    assert "General Enquiry" in html


def test_user_html_is_escaped(smtp_env):
    enquiry = dict(
        ENQUIRY,
        name="Ravi <script>alert(1)</script>",
        message="Line <b>one</b>\nLine two <script>alert(2)</script>",
    )
    msg = _build_message(enquiry["name"], "ravi@example.com", enquiry, smtp_env)
    html = _extract(msg, "text/html")
    assert "&lt;b&gt;one&lt;/b&gt;" in html
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in html
    assert "<script>" not in html
    assert "<b>one</b>" not in html
    assert "Line &lt;b&gt;one&lt;/b&gt;<br>Line two" in html


def test_headers_cannot_be_injected(smtp_env):
    enquiry = dict(ENQUIRY, name="Ravi\r\nBcc: evil@example.com")
    msg = _build_message(enquiry["name"], "ravi@example.com", enquiry, smtp_env)
    for header in ("To", "Reply-To", "Subject"):
        assert "\n" not in msg[header]
        assert "\r" not in msg[header]


def test_reference_falls_back_to_id(smtp_env):
    enquiry = dict(ENQUIRY, reference="", id=7)
    msg = _build_message("Ravi Kumar", "ravi@example.com", enquiry, smtp_env)
    assert "GV-000007" in _extract(msg, "text/html")


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def test_send_sync_uses_ssl_and_logs_in(monkeypatch, smtp_env):
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", _FakeSMTP)
    assert send_thank_you_sync("Ravi Kumar", "ravi@example.com", ENQUIRY) is True
    fake = _FakeSMTP.last
    assert fake.port == 465
    assert fake.timeout == 10
    assert fake.logged_in == ("test@gvpowers.in", "secret-password")
    assert len(fake.sent) == 1


def test_send_sync_starttls_on_587(monkeypatch, smtp_env):
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_SECURITY", "starttls")

    class _FakeStartTLS(_FakeSMTP):
        pass

    monkeypatch.setattr(mailer.smtplib, "SMTP", _FakeStartTLS)
    assert send_thank_you_sync("Ravi Kumar", "ravi@example.com", ENQUIRY) is True
    fake = _FakeStartTLS.last
    assert fake.port == 587
    assert _FakeSMTP.starttls_called is True
    assert fake.ehlo_calls == 2
    assert len(fake.sent) == 1


def test_send_sync_skips_when_unconfigured(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "")
    assert send_thank_you_sync("Ravi Kumar", "ravi@example.com", ENQUIRY) is False


def test_send_sync_returns_false_on_smtp_error(monkeypatch, smtp_env):
    def _boom(*a, **kw):
        raise ConnectionError("no network")

    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", _boom)
    monkeypatch.setattr(mailer.smtplib, "SMTP", _boom)
    assert send_thank_you_sync("Ravi Kumar", "ravi@example.com", ENQUIRY) is False


def test_connect_falls_back_to_other_gmail_port(monkeypatch, smtp_env):
    calls = []

    def _fake_connect(smtp, port, security):
        calls.append((port, security))
        if len(calls) == 1:
            raise ConnectionError("port blocked")
        return _FakeSMTP(smtp["host"], port, timeout=smtp["timeout"])

    monkeypatch.setattr(mailer, "_connect", _fake_connect)
    assert send_thank_you_sync("Ravi Kumar", "ravi@example.com", ENQUIRY) is True
    assert calls == [(465, "ssl"), (587, "starttls")]


def test_dispatch_sends_both_emails_synchronously(monkeypatch, smtp_env):
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", _FakeSMTP)
    outcome = dispatch_enquiry_emails(
        object(), "Ravi Kumar", "ravi@example.com", ENQUIRY
    )
    assert outcome == {"company": True, "customer": True}
    assert len(_FakeSMTP.all_sent) == 2


def test_dispatch_reports_partial_failure(monkeypatch, smtp_env):
    class _FailSecond(_FakeSMTP):
        def send_message(self, msg):
            if len(self.sent) == 1:
                raise smtplib.SMTPException("customer mailbox rejected")
            return super().send_message(msg)

    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", _FailSecond)
    outcome = dispatch_enquiry_emails(
        object(), "Ravi Kumar", "ravi@example.com", ENQUIRY
    )
    assert outcome == {"company": True, "customer": False}


def test_dispatch_skips_when_unconfigured(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "")
    outcome = dispatch_enquiry_emails(
        object(), "Ravi Kumar", "ravi@example.com", ENQUIRY
    )
    assert outcome == {"company": False, "customer": False}


def test_send_test_email_uses_sample_reference(monkeypatch, smtp_env):
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", _FakeSMTP)
    assert send_test_email_sync("qa@example.com") is True
    assert len(_FakeSMTP.all_sent) == 1
    html = _extract(_FakeSMTP.all_sent[0], "text/html")
    assert "GV-TEST" in html
    assert "test of the GV POWERS enquiry email flow" in html


def test_mail_test_cli(app, monkeypatch):
    import mailer as mailer_module

    monkeypatch.setattr(mailer_module, "is_configured", lambda: True)
    sent = {}

    def _fake_test(to_email, name="GV POWERS Test"):
        sent.update(to=to_email, name=name)
        return True

    monkeypatch.setattr(mailer_module, "send_test_email_sync", _fake_test)
    runner = app.test_cli_runner()
    result = runner.invoke(args=["mail-test", "--to", "qa@example.com"])
    assert result.exit_code == 0, result.output
    assert sent == {"to": "qa@example.com", "name": "GV POWERS Test"}


def test_mail_test_cli_requires_configuration(app, monkeypatch):
    import mailer as mailer_module

    monkeypatch.setattr(mailer_module, "is_configured", lambda: False)
    runner = app.test_cli_runner()
    result = runner.invoke(args=["mail-test", "--to", "qa@example.com"])
    assert result.exit_code != 0
    assert "not configured" in result.output


# ---------------------------------------------------------------------------
# Company notification
# ---------------------------------------------------------------------------

def _extract_company(smtp_env, enquiry):
    """Build the company notification and return it plus its plain/html bodies."""
    msg = _build_company_message(enquiry, smtp_env)
    return {
        "msg": msg,
        "plain": _extract(msg, "text/plain"),
        "html": _extract(msg, "text/html"),
    }


def test_company_message_reaches_gv_powers(smtp_env):
    out = _extract_company(smtp_env, ENQUIRY)
    assert "leads@gvpowers.in" in out["msg"]["To"]
    assert "New website enquiry" in out["msg"]["Subject"]
    assert "GV-000123" in out["msg"]["Subject"]
    assert out["msg"]["Date"]
    assert out["msg"]["Message-ID"]


def test_company_message_reply_to_customer(smtp_env):
    out = _extract_company(smtp_env, ENQUIRY)
    assert out["msg"]["Reply-To"] == "Ravi Kumar <ravi@example.com>"


def test_company_message_contains_lead_details(smtp_env):
    enquiry = dict(ENQUIRY, phone="+91 99887 76655")
    out = _extract_company(smtp_env, enquiry)
    assert "Ravi Kumar" in out["html"]
    assert "ravi@example.com" in out["html"]
    assert "+91 99887 76655" in out["html"]
    assert SERVICE_LABELS["solar"] in out["html"]
    assert "GV-000123" in out["html"]
    assert "Need a 5 kW rooftop system." in out["plain"]


def test_company_message_escapes_user_html(smtp_env):
    enquiry = dict(ENQUIRY, message="<img src=x onerror=alert(1)>")
    out = _extract_company(smtp_env, enquiry)
    assert "&lt;img src=x onerror=alert(1)&gt;" in out["html"]
    assert "<img src=x" not in out["html"]


def test_company_default_recipient(monkeypatch):
    monkeypatch.delenv("COMPANY_NOTIFY_EMAIL", raising=False)
    assert mailer._company_recipient() == "gvpowerssalem@gmail.com"


def test_sent_message_is_valid_mime(smtp_env):
    msg = _build_message("Ravi Kumar", "ravi@example.com", ENQUIRY, smtp_env)
    assert isinstance(msg, Message)
    assert msg.is_multipart()
