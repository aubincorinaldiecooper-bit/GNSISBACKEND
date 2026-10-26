import re

import pytest

from mcpmft.tool_protocol import MORE_WAS_CUT, ToolProtocolError, fit_tool_response, format_tool_response


def count(text: str) -> int:
    """Roughly a BPE tokenizer on JSON: words and punctuation each count."""
    return len(re.findall(r"\w+|[^\w\s]", text))


LIMIT = 256


def test_a_short_answer_is_left_alone():
    done = {"status": "done", "message": "Notes is open."}
    assert fit_tool_response(done, count, LIMIT) is done


def test_a_50_file_listing_is_cut_to_fit_and_says_more_was_cut():
    entries = [
        {"name": f"Invoice {i:02d} from the design studio.pdf", "kind": "file", "size": 1200 + i, "modified": "2026-10-2%d" % (i % 9)}
        for i in range(50)
    ]
    listing = {"status": "done", "folder": "~/Downloads", "count": 50, "entries": entries}
    assert count(format_tool_response(listing)) > LIMIT
    fitted = fit_tool_response(listing, count, LIMIT)
    assert count(format_tool_response(fitted)) <= LIMIT
    assert fitted["more"] == MORE_WAS_CUT
    assert fitted["status"] == "done" and fitted["count"] == 50
    assert 0 < len(fitted["entries"]) < 50
    assert fitted["entries"][0] == entries[0], "it keeps the start, in order"


def test_page_text_is_cut_to_fit():
    text = " ".join(f"Nene Chicken item {i}: spicy soy garlic wings, 4.6 stars, about 32 minutes, $18.99." for i in range(40))
    page = {"status": "done", "url": "https://www.ubereats.com/store/nene", "title": "Nene Chicken", "text": text}
    fitted = fit_tool_response(page, count, LIMIT)
    assert count(format_tool_response(fitted)) <= LIMIT
    assert fitted["text"].startswith("Nene Chicken item 0") and fitted["text"].endswith("…")
    assert fitted["title"] == "Nene Chicken" and fitted["more"] == MORE_WAS_CUT


def test_a_plain_string_or_a_long_message_is_cut_with_the_marker():
    fitted = fit_tool_response("word " * 600, count, LIMIT)
    assert count(format_tool_response(fitted)) <= LIMIT and fitted.endswith(f"[{MORE_WAS_CUT}]")
    long_message = fit_tool_response({"status": "failed", "message": "reason " * 600}, count, LIMIT)
    assert count(format_tool_response(long_message)) <= LIMIT
    assert long_message["status"] == "failed" and long_message["message"].endswith("…")


def test_a_budget_too_small_for_even_the_marker_still_refuses():
    with pytest.raises(ToolProtocolError):
        fit_tool_response({"status": "done", "entries": list(range(100))}, count, 5)


def test_a_long_error_without_a_message_is_cut_not_refused():
    failed = {"status": "error", "tool": "browser", "error": "navigation failed: " + "timeout waiting for selector " * 60}
    fitted = fit_tool_response(failed, count, LIMIT)
    assert count(format_tool_response(fitted)) <= LIMIT
    assert fitted["error"].startswith("navigation failed: timeout")


def test_a_single_huge_entry_is_shortened_not_dropped():
    entry = {"name": "foo", "elements": [f"button {i}: Add to cart" for i in range(200)]}
    fitted = fit_tool_response({"status": "done", "entries": [entry]}, count, LIMIT)
    assert count(format_tool_response(fitted)) <= LIMIT
    assert fitted["entries"][0]["name"] == "foo"
    assert 0 < len(fitted["entries"][0]["elements"]) < 200


def test_a_nested_state_stays_structured():
    state = {"url": "https://www.ubereats.com/store/nene", "title": "Nene Chicken", "text": "spicy wings " * 300}
    fitted = fit_tool_response({"status": "done", "state": state}, count, LIMIT)
    assert count(format_tool_response(fitted)) <= LIMIT
    assert isinstance(fitted["state"], dict)
    assert fitted["state"]["title"] == "Nene Chicken"


def test_a_long_error_and_message_keep_one_when_both_cannot_fit():
    failed = {
        "status": "failed",
        "error": "bad request " * 300,
        "message": "Try a different query. " * 300,
    }
    fitted = fit_tool_response(failed, count, 50)
    assert count(format_tool_response(fitted)) <= 50
    assert fitted["message"].startswith("Try a different")
