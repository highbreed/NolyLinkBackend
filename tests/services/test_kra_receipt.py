"""Tests for kra_receipt.py — golden-cased against real, confirmed KRA data: build_qr_link
against a real working verification link, dash_separated/format_scu_date against the real
"SCU Information" block fetched from that same link's KRA verification page (2026-10-16). See
the module's own docstrings for the sources.
"""

from app.services.kra_receipt import build_qr_link, dash_separated, format_scu_date


def test_build_qr_link_matches_confirmed_real_example():
    link = build_qr_link(tin="P051633009W", bhf_id="00", rcpt_sign="XVHTWAWXSEGRFJCN")
    assert link == (
        "https://etims-sbx.kra.go.ke/common/link/etims/receipt/"
        "indexEtimsReceiptData?Data=P051633009W00XVHTWAWXSEGRFJCN"
    )


def test_build_qr_link_uses_production_host_when_configured(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("KRA_ENV", "production")
    get_settings.cache_clear()
    try:
        link = build_qr_link(tin="P051633009W", bhf_id="00", rcpt_sign="XVHTWAWXSEGRFJCN")
        assert link == (
            "https://etims.kra.go.ke/common/link/etims/receipt/"
            "indexEtimsReceiptData?Data=P051633009W00XVHTWAWXSEGRFJCN"
        )
    finally:
        get_settings.cache_clear()


def test_build_qr_link_returns_none_without_a_signature():
    assert build_qr_link("P051633009W", "00", None) is None
    assert build_qr_link("P051633009W", "00", "") is None


def test_dash_separated_matches_real_verification_page():
    assert dash_separated("IV6JZ5FQEGR7ZMSE2MJ2KCQK7Q") == "IV6J-Z5FQ-EGR7-ZMSE-2MJ2-KCQK-7Q"
    assert dash_separated("DDF4HLFWMWDVWCGD") == "DDF4-HLFW-MWDV-WCGD"
    assert dash_separated(None) is None
    assert dash_separated("") is None


def test_format_scu_date_matches_real_verification_page():
    assert format_scu_date("20261016104905") == "16/10/2026 10:49:05"
    assert format_scu_date(None) is None
    assert format_scu_date("not14chars") is None
