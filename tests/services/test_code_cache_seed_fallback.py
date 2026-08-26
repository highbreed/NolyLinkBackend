"""Tests for the static-seed fallback (spec §20 item 2) — see
app/data/kra_code_seeds/README.md for why no real seed files are checked in yet.
"""

import json
import logging

from app.services import code_cache_service


async def test_missing_seed_files_log_a_warning_and_do_not_crash(db_session, caplog):
    with caplog.at_level(logging.WARNING):
        await code_cache_service.load_static_seed_fallback(db_session)

    warning_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    for slug in code_cache_service.LARGE_CODE_TABLE_SLUGS:
        assert any(slug in msg for msg in warning_messages), slug


async def test_seed_file_gets_upserted_when_present(db_session, tmp_path, monkeypatch):
    seed_dir = tmp_path / "kra_code_seeds"
    seed_dir.mkdir()
    (seed_dir / "currency.json").write_text(
        json.dumps([{"cd": "USD", "cdNm": "US Dollar", "srtOrd": 1, "useYn": "Y"}])
    )
    monkeypatch.setattr(code_cache_service, "SEED_DIR", seed_dir)

    await code_cache_service.load_static_seed_fallback(db_session)

    codes = await code_cache_service.get_codes(db_session, "currency")
    assert len(codes) == 1
    assert codes[0].cd == "USD"
    assert codes[0].cd_nm == "US Dollar"
