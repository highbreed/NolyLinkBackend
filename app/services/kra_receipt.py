"""Builds the QR verification link KRA requires on every printed receipt (TIS-OSCU-VSCU-v2.0
spec §6.23.8 / the spec's own "Normal Invoice" sample receipt layout).

The spec itself is internally inconsistent about what the QR code encodes — §6.23.8 describes
a "#"-delimited six-field string (invoice_date#time#cu_number#cu_receipt_number#internal_data#
receipt_signature), but the sample receipt layout a few pages later shows the QR as a
verification URL built from just three fields: KRA PIN (tin) + branch ID (bhfId) + receipt
signature. Confirmed against a real working example (2026-10-16, user-supplied):
    https://etims.kra.go.ke/common/link/etims/receipt/indexEtimsReceiptData?Data=P051633009W00XVHTWAWXSEGRFJCN
    P051633009W = tin (11 chars), 00 = bhfId (2 chars), XVHTWAWXSEGRFJCN = rcptSign (16 chars)
— the three fields concatenated directly with no separator, as the `Data` query parameter.
This is the real, scannable QR payload; the URL path is spelled "indexEtimsReceiptData" (the
PDF's own "indexEtimsReceptData" is a typo, not a second real endpoint).

The host itself differs between sandbox and production (confirmed: a sandbox-fiscalized
invoice's verification page only resolves at the "-sbx" host) — see
Settings.kra_receipt_verification_base_url / KRA_RECEIPT_VERIFICATION_BASE_URL_PROD/SBX in
.env.example, selected by KRA_ENV same as kra_base_url.
"""

from app.core.config import Settings, get_settings


def build_qr_link(
    tin: str, bhf_id: str, rcpt_sign: str | None, settings: Settings | None = None
) -> str | None:
    """None if rcpt_sign isn't available yet (e.g. a VSCU sale that's SIGNED_PENDING_
    TRANSMISSION may still carry a real rcptSign — see vscu_sales_service.py — so this only
    returns None for the genuinely-no-signature-at-all case, not based on fiscalization status).
    """
    if not rcpt_sign:
        return None
    base_url = (settings or get_settings()).kra_receipt_verification_base_url
    return f"{base_url}?Data={tin}{bhf_id}{rcpt_sign}"


def dash_separated(value: str | None) -> str | None:
    """KRA's receipt convention (spec §6.23.6/§6.23.7: "Internal data/Receipt signature
    separate by dash after every 4th character") — confirmed exactly against a real KRA
    verification page (fetched 2026-10-16, the same invoice build_qr_link's docstring example
    comes from): "IV6JZ5FQEGR7ZMSE2MJ2KCQK7Q" displays as "IV6J-Z5FQ-EGR7-ZMSE-2MJ2-KCQK-7Q",
    "DDF4HLFWMWDVWCGD" as "DDF4-HLFW-MWDV-WCGD" — plain 4-character chunking, last group short
    if the length isn't a multiple of 4.
    """
    if not value:
        return None
    return "-".join(value[i : i + 4] for i in range(0, len(value), 4))


def format_scu_date(sdc_datetime: str | None) -> str | None:
    """sdcDateTime on the wire is "YYYYMMDDHHMMSS" (spec §6.6). The verification page's own
    "SCU Information > Date" field displays it as "DD/MM/YYYY HH:MM:SS" — confirmed against
    the same real receipt as dash_separated above ("20261016104905" -> "16/10/2026 10:49:05").
    """
    if not sdc_datetime or len(sdc_datetime) != 14:
        return None
    year, month, day = sdc_datetime[0:4], sdc_datetime[4:6], sdc_datetime[6:8]
    hour, minute, second = sdc_datetime[8:10], sdc_datetime[10:12], sdc_datetime[12:14]
    return f"{day}/{month}/{year} {hour}:{minute}:{second}"
