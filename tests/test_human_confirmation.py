from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
from anki.collection import Collection
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ElicitRequestParams, ElicitResult, ErrorData

from anki_mcp import app as app_module
from anki_mcp.app import create_app
from anki_mcp.config import Settings

_Callback = Callable[[Any, ElicitRequestParams], Awaitable[ElicitResult | ErrorData]]


@pytest.fixture
def collection_path(tmp_path: Path) -> Path:
    path = tmp_path / "collection.anki2"
    collection = Collection(str(path))
    try:
        deck_id = collection.decks.id("Charter")
        note = collection.new_note(collection.models.current())
        note["Front"] = "front"
        note["Back"] = "back"
        note.tags = ["doomed"]
        collection.add_note(note, deck_id)
    finally:
        collection.close()
    return path


@asynccontextmanager
async def _client(
    monkeypatch: pytest.MonkeyPatch,
    collection_path: Path,
    *,
    mode: str = "token",
    scopes: str = "read,write,admin,destructive",
    elicitation: _Callback | None = None,
) -> AsyncIterator[ClientSession]:
    monkeypatch.setenv("MCP_AUTH_TOKEN", "confirmation-token")
    monkeypatch.setenv("ANKI_COLLECTION_PATH", str(collection_path))
    monkeypatch.setenv("ANKI_SYNC_ON_WRITE", "false")
    monkeypatch.setenv("MCP_SCOPES", scopes)
    monkeypatch.setenv("ANKI_ALLOW_DESTRUCTIVE", "true")
    monkeypatch.setenv("ANKI_ALLOW_SCHEMA_CHANGES", "true")
    monkeypatch.setenv("ANKI_ALLOW_FULL_SYNC", "true")
    monkeypatch.setenv("ANKI_CONFIRMATION_MODE", mode)
    monkeypatch.setenv("ANKI_CONFIRMATION_TTL_SECONDS", "30")
    app: Any = create_app(Settings(_env_file=None))
    while not hasattr(app, "state"):
        app = app.app
    async with (
        app.state.collection_service,
        create_connected_server_and_client_session(
            app.state.mcp, elicitation_callback=elicitation
        ) as session,
    ):
        yield session


async def _call(session: ClientSession, name: str, arguments: dict[str, Any]) -> Any:
    result = await session.call_tool(name, arguments)
    text = result.content[0].text  # type: ignore[union-attr]
    return result.isError, json.loads(text[text.index("{") :])


async def _delete_tag(session: ClientSession) -> tuple[bool, Any]:
    _, preview = await _call(session, "anki_tags_delete_preview", {"name": "doomed"})
    return await _call(
        session,
        "anki_tags_delete",
        {"name": "doomed", "confirmation_token": preview["confirmation_token"]},
    )


async def _tag_exists(session: ClientSession) -> bool:
    _, tags = await _call(session, "anki_tags_list", {})
    return "doomed" in json.dumps(tags)


def _answer(result: ElicitResult) -> tuple[_Callback, list[ElicitRequestParams]]:
    seen: list[ElicitRequestParams] = []

    async def callback(_: Any, params: ElicitRequestParams) -> ElicitResult:
        seen.append(params)
        return result

    return callback, seen


@pytest.mark.anyio
async def test_accept_runs_the_mutation_after_showing_the_impact(
    monkeypatch: pytest.MonkeyPatch, collection_path: Path
) -> None:
    callback, seen = _answer(ElicitResult(action="accept", content={"confirm": True}))
    async with _client(monkeypatch, collection_path, mode="elicit", elicitation=callback) as s:
        is_error, _ = await _delete_tag(s)
        assert is_error is False
        assert not await _tag_exists(s)
    assert len(seen) == 1
    assert "anki_tags_delete" in seen[0].message
    assert "impact" in seen[0].message


@pytest.mark.anyio
@pytest.mark.parametrize(
    "answer",
    [
        ElicitResult(action="decline"),
        ElicitResult(action="cancel"),
        ElicitResult(action="accept", content={"confirm": False}),
    ],
)
async def test_anything_but_an_affirmative_accept_blocks_the_mutation(
    monkeypatch: pytest.MonkeyPatch, collection_path: Path, answer: ElicitResult
) -> None:
    callback, _ = _answer(answer)
    async with _client(monkeypatch, collection_path, mode="elicit", elicitation=callback) as s:
        is_error, payload = await _delete_tag(s)
        assert is_error is True
        assert payload["code"] == "CONFIRMATION_DECLINED"
        assert await _tag_exists(s)


@pytest.mark.anyio
async def test_unanswered_prompt_times_out_as_a_decline(
    monkeypatch: pytest.MonkeyPatch, collection_path: Path
) -> None:
    async def late(_: Any, __: ElicitRequestParams) -> ElicitResult:
        await anyio.sleep(0.5)
        return ElicitResult(action="accept", content={"confirm": True})

    monkeypatch.setattr(app_module, "move_on_after", lambda _: anyio.move_on_after(0.05))
    async with _client(monkeypatch, collection_path, mode="elicit", elicitation=late) as s:
        is_error, payload = await _delete_tag(s)
        assert is_error is True
        assert payload["code"] == "CONFIRMATION_DECLINED"
        assert await _tag_exists(s)


@pytest.mark.anyio
async def test_client_without_elicitation_falls_back_to_the_token_in_elicit_mode(
    monkeypatch: pytest.MonkeyPatch, collection_path: Path
) -> None:
    async with _client(monkeypatch, collection_path, mode="elicit") as s:
        is_error, _ = await _delete_tag(s)
        assert is_error is False
        assert not await _tag_exists(s)


@pytest.mark.anyio
async def test_client_without_elicitation_is_refused_in_elicit_required_mode(
    monkeypatch: pytest.MonkeyPatch, collection_path: Path
) -> None:
    async with _client(monkeypatch, collection_path, mode="elicit_required") as s:
        is_error, payload = await _delete_tag(s)
        assert is_error is True
        assert payload["code"] == "DESTRUCTIVE_CONFIRMATION_REQUIRED"
        assert await _tag_exists(s)


@pytest.mark.anyio
async def test_tools_carry_annotations(
    monkeypatch: pytest.MonkeyPatch, collection_path: Path
) -> None:
    async with _client(monkeypatch, collection_path) as s:
        tools = {tool.name: tool for tool in (await s.list_tools()).tools}

    def hints(name: str) -> tuple[bool | None, bool | None, bool | None, str]:
        tool = tools[name]
        assert tool.annotations is not None
        assert tool.meta is not None
        return (
            tool.annotations.readOnlyHint,
            tool.annotations.idempotentHint,
            tool.annotations.destructiveHint,
            tool.meta["anki-mcp/sync"],
        )

    assert hints("anki_decks_list") == (True, True, False, "none")
    assert hints("anki_tags_delete_preview") == (True, True, False, "none")
    assert hints("anki_note_types_change_preview") == (True, True, False, "none")
    assert hints("anki_notes_create") == (False, False, False, "incremental")
    assert hints("anki_cards_reposition") == (False, False, False, "incremental")
    assert hints("anki_tags_delete") == (False, False, True, "incremental")
    assert hints("anki_note_types_create") == (False, False, False, "incremental")
    assert hints("anki_note_types_update") == (False, False, True, "incremental")
    assert hints("anki_templates_update") == (False, False, True, "conditional")
    assert hints("anki_sync_full_upload") == (False, False, True, "full")


@pytest.mark.anyio
async def test_write_session_sets_deck_description_and_repositions_cards(
    monkeypatch: pytest.MonkeyPatch, collection_path: Path
) -> None:
    async with _client(monkeypatch, collection_path, scopes="read,write") as s:
        _, decks = await _call(s, "anki_decks_list", {})
        deck_id = next(d["id"] for d in decks["items"] if d["name"] == "Charter")
        is_error, _ = await _call(
            s, "anki_decks_update", {"deck_id": deck_id, "description": "What this deck is for"}
        )
        assert is_error is False
        await _call(s, "anki_decks_update", {"deck_id": deck_id, "name": "Charter Renamed"})
        _, deck = await _call(s, "anki_decks_get", {"deck_id": deck_id})
        assert deck["name"] == "Charter Renamed"
        assert deck["description"] == "What this deck is for"
        is_error, payload = await _call(s, "anki_decks_update", {"deck_id": deck_id})
        assert is_error is True
        assert payload["code"] == "INVALID_ARGUMENT"

        _, cards = await _call(s, "anki_cards_search", {"query": "deck:*"})
        card_id = cards["items"][0]["id"]
        is_error, _ = await _call(
            s, "anki_cards_reposition", {"card_ids": [card_id], "starting_from": 5}
        )
        assert is_error is False


def _schema_marker(path: Path) -> int:
    collection = Collection(str(path))
    try:
        return int(collection.db.scalar("select scm from col"))
    finally:
        collection.close()


async def _update_templates(
    session: ClientSession, *, add_template: bool, idempotency_key: str
) -> tuple[bool, Any, Any, dict[str, Any]]:
    _, types = await _call(session, "anki_note_types_list", {})
    note_type_id = next(t["id"] for t in types["items"] if t["name"] == "Basic")
    _, note_type = await _call(session, "anki_note_types_get", {"note_type_id": note_type_id})
    mappings: list[dict[str, Any]] = [
        {
            "source_ordinal": t["ordinal"],
            "name": t["name"],
            "question_format": t["question_format"] + " edited",
            "answer_format": t["answer_format"],
        }
        for t in note_type["templates"]
    ]
    if add_template:
        mappings.append(
            {
                "source_ordinal": None,
                "name": "Reverse",
                "question_format": "{{Back}}",
                "answer_format": "{{Front}}",
            }
        )
    _, preview = await _call(
        session,
        "anki_note_types_change_preview",
        {
            "operation": "templates_update",
            "note_type_id": note_type_id,
            "template_mappings": mappings,
        },
    )
    arguments = {
        "note_type_id": note_type_id,
        "mappings": mappings,
        "confirmation_token": preview["confirmation_token"],
        "idempotency_key": idempotency_key,
    }
    is_error, payload = await _call(session, "anki_templates_update", arguments)
    return is_error, payload, preview["impact"]["full_sync_required"], arguments


@pytest.mark.anyio
async def test_template_text_edit_syncs_normally_without_asking(
    monkeypatch: pytest.MonkeyPatch, collection_path: Path
) -> None:
    before = _schema_marker(collection_path)
    callback, seen = _answer(ElicitResult(action="decline"))
    async with _client(monkeypatch, collection_path, mode="token", elicitation=callback) as s:
        is_error, _, full_sync, _ = await _update_templates(
            s, add_template=False, idempotency_key="text-edit"
        )
        assert is_error is False
        assert full_sync is False
    assert seen == []
    assert _schema_marker(collection_path) == before


@pytest.mark.anyio
async def test_structural_change_asks_even_in_token_mode(
    monkeypatch: pytest.MonkeyPatch, collection_path: Path
) -> None:
    before = _schema_marker(collection_path)
    callback, seen = _answer(ElicitResult(action="accept", content={"confirm": True}))
    async with _client(monkeypatch, collection_path, mode="token", elicitation=callback) as s:
        is_error, first, full_sync, arguments = await _update_templates(
            s, add_template=True, idempotency_key="add-template"
        )
        assert is_error is False
        assert full_sync is True
        assert len(seen) == 1
        assert "Sync every other device" in seen[0].message
        is_error, replay = await _call(s, "anki_templates_update", arguments)
        assert (is_error, replay) == (False, first)
    assert len(seen) == 1, "a replay mutates nothing, so it does not ask again"
    assert _schema_marker(collection_path) != before


@pytest.mark.anyio
async def test_clients_that_cannot_ask_get_steps_to_make_the_change_in_anki(
    monkeypatch: pytest.MonkeyPatch, collection_path: Path
) -> None:
    before = _schema_marker(collection_path)
    async with _client(monkeypatch, collection_path, mode="token") as s:
        is_error, payload, _, _ = await _update_templates(
            s, add_template=True, idempotency_key="refused"
        )
        assert is_error is True
        assert payload["code"] == "MANUAL_CHANGE_REQUIRED"
    assert _schema_marker(collection_path) == before
    steps = payload["details"]
    assert steps["note_type"] == "Basic"
    assert [(t["name"], t["change"]) for t in steps["templates"]] == [
        ("Card 1", "keep"),
        ("Reverse", "add"),
    ]
    assert steps["templates"][1]["front"] == "{{Back}}"
    assert steps["remove"] == []
    assert any("Manage Note Types" in step for step in steps["steps"])


@pytest.mark.anyio
async def test_declined_structural_change_does_not_run(
    monkeypatch: pytest.MonkeyPatch, collection_path: Path
) -> None:
    before = _schema_marker(collection_path)
    callback, _ = _answer(ElicitResult(action="decline"))
    async with _client(monkeypatch, collection_path, mode="token", elicitation=callback) as s:
        is_error, payload, _, _ = await _update_templates(
            s, add_template=True, idempotency_key="declined"
        )
        assert is_error is True
        assert payload["code"] == "CONFIRMATION_DECLINED"
    assert _schema_marker(collection_path) == before


def test_manual_steps_describe_moves_renames_and_removals() -> None:
    note_type = {
        "name": "Vocab",
        "fields": [
            {"name": "Word", "ordinal": 0},
            {"name": "Meaning", "ordinal": 1},
            {"name": "Notes", "ordinal": 2},
        ],
    }
    steps = app_module._manual_note_type_steps(  # pyright: ignore[reportPrivateUsage]
        "anki_note_type_fields_update",
        note_type,
        [
            {"name": "Definition", "source_ordinal": 1},
            {"name": "Word", "source_ordinal": 0},
            {"name": "Example", "source_ordinal": None},
        ],
    )
    assert [(f["name"], f["change"]) for f in steps["fields"]] == [
        ("Definition", "move from position 2, rename from Meaning"),
        ("Word", "move from position 1"),
        ("Example", "add"),
    ]
    assert steps["remove"] == ["Notes"]
    assert "front" not in steps["fields"][0]
