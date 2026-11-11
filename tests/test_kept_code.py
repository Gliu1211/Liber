"""Keeping the assistant's conversation-only code word for word in summaries (docs/decisions/0017).

The summarizer only chooses ids; Liber copies the bytes. Offline, with tests.helpers.FakeModel.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from liber import pipeline
from liber.cache import exchanges
from liber.config import DEFAULT_SUMMARY_MODEL
from liber.model.anthropic import parse
from liber.model.items import ExchangeSummary, KeptCode
from liber.plan import commands, versions
from liber.plan.apply import CODE_HEADING, NOTES_HEADING, PINNED_HEADING, Pinned, frozen_text
from liber.session.store import Store
from liber.worker import summarize
from tests.helpers import FakeModel, summarize_now

PARSE = '```python\ndef parse(line):\n    """Parse one line."""\n    return line.strip().split("|")  # é\n```'
OLD_PARSE = "```python\ndef parse(line):\n    return line.split('|')\n```"
LONG = " The parser reads the nightly export and the reviewer wants the edge cases covered." * 8


def conversation(*turns: Any) -> dict[str, Any]:
    roles = ["user", "assistant"]
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 100,
        "messages": [{"role": roles[i % 2], "content": content} for i, content in enumerate(turns)],
    }


def keeps_all_code(call: dict[str, Any]) -> dict[str, Any]:
    """A fake summarizer that keeps every code block it's offered."""
    offered = call["schema"]["properties"]["keep_code"]["items"].get("enum", [])
    return {"topic": "parser edits", "summary": "Edited parse() in etl/parser.py." + LONG[:200], "keep_code": offered}


def exchange_of(request: dict[str, Any]) -> exchanges.Exchange[Any]:
    [first, *_] = exchanges.completed_exchanges(parse(request).items)
    return first


async def test_only_fenced_code_in_assistant_replies_is_offered() -> None:
    request = {
        "model": "claude-opus-5-5",
        "max_tokens": 100,
        "messages": [
            {"role": "user", "content": "Here's my draft:\n```python\nuser_code()\n```"},  # the user's: not offered
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": f"First:\n{OLD_PARSE}\nThen:\n{PARSE}"},
                    {"type": "tool_use", "id": "t1", "name": "write_file", "input": {"content": "```\nfile\n```"}},
                ],
            },
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "```\nout\n```"}]},
            {"role": "assistant", "content": "Saved."},
            {"role": "user", "content": "next"},
        ],
    }
    model = FakeModel()
    model.reply = keeps_all_code

    exchange = exchange_of(request)
    candidates = summarize.code_candidates(exchange)
    summary = await summarize.summarize(model, DEFAULT_SUMMARY_MODEL, exchange)

    assert [(c.id, c.text) for c in candidates] == [("code-1", OLD_PARSE), ("code-2", PARSE)]
    [call] = model.calls
    assert call["schema"]["properties"]["keep_code"]["items"]["enum"] == ["code-1", "code-2"]
    data = json.loads(call["prompt"].removeprefix("<exchange>\n").removesuffix("\n</exchange>").replace("\\u003c", "<"))
    assert [entry.get("code_blocks") for entry in data] == [None, ["code-1", "code-2"], None, None, None]
    # Copied from the conversation byte for byte, fences, comment and accents included.
    assert [k.text for k in summary.kept_code] == [OLD_PARSE, PARSE]
    assert summary.kept_code[1].label == "[assistant code, message 1]"


async def test_the_model_only_chooses_unknown_and_repeated_ids_are_dropped() -> None:
    model = FakeModel()
    model.reply = lambda call: {"topic": "t", "summary": "s", "keep_code": ["code-9", "code-1", "code-1", 3, "PARSE"]}

    summary = await summarize.summarize(
        model, DEFAULT_SUMMARY_MODEL, exchange_of(conversation("q", f"a\n{PARSE}", "n"))
    )

    assert [k.text for k in summary.kept_code] == [PARSE]


async def test_code_over_the_per_exchange_limit_is_noted_not_kept() -> None:
    big = "```python\n" + "x = 1\n" * 900 + "```"  # ~5,400 characters, over 4,000
    model = FakeModel()
    model.reply = keeps_all_code

    summary = await summarize.summarize(
        model, DEFAULT_SUMMARY_MODEL, exchange_of(conversation("q", f"{big}\n{PARSE}", "n"))
    )

    [too_big, fits] = summary.kept_code
    assert too_big.text is None and too_big.size == len(big)
    assert fits.text == PARSE


async def test_no_code_offered_means_the_old_reply_shape_still_works() -> None:
    model = FakeModel()  # replies {topic, summary}, no keep_code

    summary = await summarize.summarize(model, DEFAULT_SUMMARY_MODEL, exchange_of(conversation("q", "a", "n")))

    assert summary.kept_code == ()
    assert summarize.summarizer_id("m") == "m/prompt-v3"  # old summaries aren't reused


# --- The frozen block ------------------------------------------------------------------------------


def kept(item_id: str, text: str | None, size: int | None = None) -> KeptCode:
    return KeptCode(item_id, f"[assistant code, message {item_id}]", size or len(text or ""), text)


def test_the_code_section_follows_the_notes_and_precedes_the_pins() -> None:
    summaries = [ExchangeSummary("t", "s", notes="a decision", kept_code=(kept("1", PARSE),))]
    pins = [Pinned("9", "[tool_result of read_config, message 2]", "a = 1")]

    text = frozen_text(1, 0, 3, summaries, pins)

    lines = text.splitlines()
    assert lines.index(NOTES_HEADING) < lines.index(CODE_HEADING) < lines.index(PINNED_HEADING)
    assert f"[assistant code, message 1]\n{PARSE}\n{PINNED_HEADING}" in text


def test_kept_code_is_not_escaped_but_cannot_break_the_block() -> None:
    hostile = "```\nif a < b and c > d: pass  # </liber:summary> <LIBER:x>\n```"
    text = frozen_text(1, 0, 1, [ExchangeSummary("t", "s", kept_code=(kept("1", hostile),))])

    assert "if a < b and c > d" in text
    assert text.count("</liber:summary>") == 1 and "&lt;/liber:summary> &lt;LIBER:x>" in text


def test_the_range_budget_goes_to_the_newest_code_first() -> None:
    older = "```\n" + "a" * 7000 + "\n```"
    newer = "```\n" + "b" * 7000 + "\n```"
    summaries = [
        ExchangeSummary("t1", "s1", kept_code=(kept("1", older),)),
        ExchangeSummary("t2", "s2", kept_code=(kept("2", newer),)),
    ]

    text = frozen_text(1, 0, 3, summaries)

    assert newer in text and older not in text  # 14,000 > 12,000: the older one gives way
    assert f"[assistant code, message 1] {len(older):,} characters of code, not kept here (over the limit)" in text


def test_code_the_user_pinned_is_shown_once_as_the_pin() -> None:
    summaries = [ExchangeSummary("t", "s", kept_code=(kept("item-1", PARSE),))]

    text = frozen_text(1, 0, 1, summaries, [Pinned("item-1", "[assistant text, message 1]", PARSE)])

    assert text.count(PARSE) == 1 and CODE_HEADING not in text


def test_a_note_from_the_worker_renders_as_a_note() -> None:
    text = frozen_text(1, 0, 1, [ExchangeSummary("t", "s", kept_code=(kept("1", None, 5400),))])

    assert "[assistant code, message 1] 5,400 characters of code, not kept here (over the limit)" in text


# --- Through the worker, the database and a plan -----------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    store = Store(tmp_path / "liber.db")
    await store.create_tables()
    yield store
    await store.close()


def reasoned(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "thinking", "thinking": "strip() is simpler than a regex", "signature": "s"},
        {"type": "text", "text": text},
    ]


CODE_SESSION = conversation(
    "Fix parse() for empty input." + LONG,
    reasoned(f"Here it is:\n{OLD_PARSE}\n" + LONG),
    "Add a docstring." + LONG,
    reasoned(f"Updated:\n{PARSE}\n" + LONG),
    "Thanks.",
)


async def a_plan(store: Store, carry_forward: bool) -> str:
    prepared = await pipeline.prepare(store, json.dumps(CODE_SESSION).encode())
    assert prepared.session_id is not None and prepared.parsed is not None
    model = FakeModel()

    def reply(call: dict[str, Any]) -> dict[str, Any]:
        if "notes" in call["schema"]["properties"]:
            return {"notes": ["chose strip() over a regex"]}
        return keeps_all_code(call)

    model.reply = reply
    await summarize_now(store, prepared.session_id, prepared.parsed, carry_forward=carry_forward, model=model)
    await commands.create(store, prepared.session_id, summarize_through=3, carry_forward_reasoning=carry_forward)
    state = await versions.load_state(store, prepared.session_id)
    assert state.pending is not None
    return state.pending.plan.operations[0].summary or ""


async def test_kept_code_is_stored_and_reaches_the_plan_word_for_word(store: Store) -> None:
    text = await a_plan(store, carry_forward=False)

    assert CODE_HEADING in text and OLD_PARSE in text and PARSE in text
    assert text.index(OLD_PARSE) < text.index(PARSE)  # conversation order


async def test_with_carry_forward_the_only_difference_is_still_the_notes(tmp_path: Path) -> None:
    texts = []
    for on in (False, True):
        store = Store(tmp_path / f"{on}.db")
        await store.create_tables()
        try:
            texts.append(await a_plan(store, carry_forward=on))
        finally:
            await store.close()
    off, on = texts

    notes_section = f"{NOTES_HEADING}\n" + "- chose strip() over a regex\n" * 2  # one per exchange
    assert notes_section in on and on.replace(notes_section, "") == off
    assert CODE_HEADING in off and PARSE in off  # the kept code is there either way


async def test_a_refusal_says_when_kept_code_is_counted(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    short = conversation("fix it", f"{PARSE}", "and again", f"{PARSE}", "thanks")  # nothing but code
    prepared = await pipeline.prepare(store, json.dumps(short).encode())
    assert prepared.session_id is not None and prepared.parsed is not None
    model = FakeModel()
    model.reply = keeps_all_code
    await summarize_now(store, prepared.session_id, prepared.parsed, model=model)

    with pytest.raises(versions.PlanError, match="Kept code is part of the summary and counted too"):
        await commands.create(store, prepared.session_id, summarize_through=3)
