from __future__ import annotations

import logging

from roomieorder.logutil import correlated


def test_correlated_prefixes_message(caplog) -> None:  # type: ignore[no-untyped-def]
    logger = logging.getLogger("roomieorder.test.corr")
    log = correlated(logger, provider="costco", item="paper_towels")
    with caplog.at_level(logging.INFO, logger="roomieorder.test.corr"):
        log.info("reached checkout")
    assert "[provider=costco item=paper_towels] reached checkout" in caplog.text


def test_correlated_drops_empty_fields() -> None:
    logger = logging.getLogger("roomieorder.test.corr")
    log = correlated(logger, row=7, item="dish_soap", provider="")
    # Empty/None fields are omitted; the rest keep insertion order.
    msg, _ = log.process("hi", {})
    assert msg == "[row=7 item=dish_soap] hi"


def test_correlated_no_token_passes_message_through() -> None:
    logger = logging.getLogger("roomieorder.test.corr")
    log = correlated(logger, provider="", item=None)
    msg, _ = log.process("plain", {})
    assert msg == "plain"
