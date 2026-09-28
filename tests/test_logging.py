import logging

from app.logging_setup import RedactingFilter, redact


def test_redact_patterns_and_explicit_secrets():
    token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1"
    text = f"url https://api.telegram.org/bot{token}/getMe key sk-ant-api03-abcdefghijklmnop mysecret"
    out = redact(text, ["mysecret"])
    assert token not in out
    assert "sk-ant-api03" not in out
    assert "mysecret" not in out


def test_filter_masks_args():
    f = RedactingFilter(["topsecret"])
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "value=%s", ("topsecret",), None)
    f.filter(record)
    assert record.getMessage() == "value=***"
