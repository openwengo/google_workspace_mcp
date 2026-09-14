"""Tests for bare-newline handling in HTML bodies (html_newlines_to_br)."""

import base64
from email import message_from_bytes
from email.policy import SMTP

import pytest

from gmail.gmail_helpers import html_newlines_to_br
from gmail.gmail_tools import _prepare_gmail_message

SIGNATURE_HTML = (
    '<div dir="ltr"><div><blockquote style="margin:0px 0px 0px 15px">'
    "<b>Jane Doe</b></blockquote><blockquote>Engineer</blockquote></div>"
    "<div><br></div></div>"
)


def _decode_parts(raw_b64: str) -> dict:
    msg = message_from_bytes(base64.urlsafe_b64decode(raw_b64), policy=SMTP)
    parts = {}
    for part in msg.walk():
        if part.get_content_type().startswith("text/"):
            # SMTP policy emits CRLF; compare against the LF the caller passed.
            parts[part.get_content_type()] = part.get_content().replace("\r\n", "\n")
    return parts


class TestHtmlNewlinesToBr:
    def test_bare_paragraphs_get_breaks(self):
        body = "Hi Jane,\n\nFirst paragraph.\nSecond line.\n\nRegards,\nJohn"
        assert html_newlines_to_br(body) == (
            "Hi Jane,<br><br>\nFirst paragraph.<br>\nSecond line.<br><br>\nRegards,<br>\nJohn"
        )

    def test_well_formed_html_is_untouched(self):
        body = (
            "<p>Hi Jane,</p>\n<p>Thanks.</p>\n<ul>\n<li>one</li>\n<li>two</li>\n</ul>"
        )
        assert html_newlines_to_br(body) == body

    def test_compact_html_is_untouched(self):
        body = "<p>Hi</p><p>Bye</p>"
        assert html_newlines_to_br(body) == body

    def test_newline_inside_paragraph_becomes_break(self):
        body = "<p>Hi,\nhow are you</p>\n<p>fine</p>"
        assert html_newlines_to_br(body) == "<p>Hi,<br>\nhow are you</p>\n<p>fine</p>"

    def test_inline_tags_do_not_swallow_breaks(self):
        body = "<b>Note:</b>\nnext line\n\n<a href='x'>link</a>\nend"
        assert html_newlines_to_br(body) == (
            "<b>Note:</b><br>\nnext line<br><br>\n<a href='x'>link</a><br>\nend"
        )

    def test_appended_signature_survives(self):
        body = "Hi,\nline two<br><br>" + SIGNATURE_HTML
        assert html_newlines_to_br(body) == "Hi,<br>\nline two<br><br>" + SIGNATURE_HTML

    def test_runs_of_newlines_cap_at_two_breaks(self):
        assert html_newlines_to_br("a\n\n\n\nb") == "a<br><br>\nb"

    def test_pre_blocks_are_never_touched(self):
        body = "<p>Log:</p>\n<pre>line 1\nline 2</pre>\nafter\nmore"
        assert html_newlines_to_br(body) == body

    @pytest.mark.parametrize("body", ["", "no newlines here", "<p>one</p>"])
    def test_noop_inputs(self, body):
        assert html_newlines_to_br(body) == body


class TestPrepareGmailMessageNewlines:
    def test_html_body_with_bare_newlines_renders_breaks(self):
        raw_b64, _, _, _ = _prepare_gmail_message(
            subject="Test",
            body="Hi Jane,\n\nFirst paragraph.\nSecond line.",
            to="jane@example.com",
            body_format="html",
        )
        parts = _decode_parts(raw_b64)
        assert (
            parts["text/html"].strip()
            == "Hi Jane,<br><br>\nFirst paragraph.<br>\nSecond line."
        )
        # The text/plain alternative keeps the breaks as real newlines.
        assert (
            parts["text/plain"].strip() == "Hi Jane,\n\nFirst paragraph.\nSecond line."
        )

    def test_well_formed_html_body_is_unchanged(self):
        body = "<p>Hi Jane,</p>\n<p>Thanks.</p>"
        raw_b64, _, _, _ = _prepare_gmail_message(
            subject="Test", body=body, to="jane@example.com", body_format="html"
        )
        assert _decode_parts(raw_b64)["text/html"].strip() == body

    def test_plain_body_is_unaffected(self):
        body = "Hi Jane,\n\nplain text stays plain."
        raw_b64, _, _, _ = _prepare_gmail_message(
            subject="Test", body=body, to="jane@example.com", body_format="plain"
        )
        parts = _decode_parts(raw_b64)
        assert "text/html" not in parts
        assert parts["text/plain"].strip() == body
