from __future__ import annotations

import base64
import json
import logging
import sqlite3
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from anki.collection import Collection, ImportAnkiPackageRequest
from anki.errors import NetworkError
from anki.sync import SyncAuth, SyncOutput
from anki.sync_pb2 import MediaSyncProgress, MediaSyncStatusResponse

from anki_mcp.collection import AnkiCollectionService, CollectionAdapter, MediaSyncInProgressError
from anki_mcp.state import PersistentState


@pytest.fixture
def phase2_collection(tmp_path: Path) -> Iterator[str]:
    path = str(tmp_path / "collection.anki2")
    collection = Collection(path)
    try:
        deck_id = collection.decks.id("Phase 2")
        for index in range(2):
            note = collection.new_note(collection.models.current())
            note["Front"] = f"phase two {index}"
            note["Back"] = "answer"
            collection.add_note(note, deck_id)
    finally:
        collection.close()
    yield path


@pytest.mark.anyio
async def test_deck_configuration_updates_supported_bounded_options(
    phase2_collection: str,
) -> None:
    collection = Collection(phase2_collection)
    try:
        deck_id = int(collection.decks.id_for_name("Phase 2") or 0)
    finally:
        collection.close()

    async with AnkiCollectionService(phase2_collection, max_page_size=100) as service:
        updated = await service.update_deck_config(
            deck_id,
            new_cards_per_day=17,
            reviews_per_day=123,
            max_answer_seconds=45,
            desired_retention=0.91,
        )
        deck = await service.get_deck(deck_id)
        with pytest.raises(ValueError, match="at least one"):
            await service.update_deck_config(deck_id)
        with pytest.raises(ValueError, match="desired_retention"):
            await service.update_deck_config(deck_id, desired_retention=1.5)

    assert updated == {"id": deck_id, "config_id": deck["config_id"], "updated": True}
    assert deck["config"] == {
        "name": "Default",
        "name_truncated": False,
        "new_cards_per_day": 17,
        "reviews_per_day": 123,
        "max_answer_seconds": 45,
        "desired_retention": 0.91,
    }


@pytest.mark.anyio
async def test_explicit_field_and_template_mappings_add_remove_rename_and_reorder(
    phase2_collection: str,
) -> None:
    async with AnkiCollectionService(phase2_collection, max_page_size=100) as service:
        created = await service.create_note_type(
            "Mapped",
            ["One", "Two", "Remove Me"],
            [
                {"name": "First", "question_format": "{{One}}", "answer_format": "{{Two}}"},
                {"name": "Remove Card", "question_format": "{{Two}}", "answer_format": "x"},
            ],
            "",
        )
        note_type_id = created["id"]
        fields = await service.update_note_type_fields(
            note_type_id,
            [
                {"name": "Second", "source_ordinal": 1},
                {"name": "Added", "source_ordinal": None},
                {"name": "First", "source_ordinal": 0},
            ],
        )
        templates = await service.update_templates(
            note_type_id,
            [
                {
                    "name": "Added Card",
                    "source_ordinal": None,
                    "question_format": "{{Added}}",
                    "answer_format": "{{First}}",
                },
                {
                    "name": "Renamed First",
                    "source_ordinal": 0,
                    "question_format": "{{First}}",
                    "answer_format": "{{Second}}",
                },
            ],
        )
        fetched = await service.get_note_type(note_type_id)

    assert fields == {"id": note_type_id, "updated": True, "field_count": 3}
    assert templates == {"id": note_type_id, "updated": True, "template_count": 2}
    assert [field["name"] for field in fetched["fields"]] == ["Second", "Added", "First"]
    assert [template["name"] for template in fetched["templates"]] == [
        "Added Card",
        "Renamed First",
    ]
    assert fetched["templates"][1]["answer_format"] == "{{Second}}"


@pytest.mark.anyio
async def test_card_flags_and_repositioning_are_bounded_bulk_operations(
    phase2_collection: str,
) -> None:
    collection = Collection(phase2_collection)
    try:
        card_ids = [int(card_id) for card_id in collection.find_cards('deck:"Phase 2"')]
    finally:
        collection.close()

    async with AnkiCollectionService(
        phase2_collection, max_page_size=100, max_batch_size=2
    ) as service:
        flagged = await service.set_card_flag(card_ids, 3)
        repositioned = await service.reposition_cards(
            card_ids,
            starting_from=10,
            step_size=2,
            randomize=False,
            shift_existing=True,
        )
        cards = [await service.get_card(card_id) for card_id in card_ids]
        with pytest.raises(ValueError, match="flag"):
            await service.set_card_flag(card_ids, 8)
        with pytest.raises(ValueError, match="starting_from"):
            await service.reposition_cards(
                card_ids,
                starting_from=0,
                step_size=1,
                randomize=False,
                shift_existing=True,
            )

    assert flagged == {"card_ids": card_ids, "flag": 3, "updated": len(card_ids)}
    assert repositioned == {"card_ids": card_ids, "repositioned": len(card_ids)}
    assert {card["flags"] for card in cards} == {3}
    assert sorted(card["scheduling"]["due"] for card in cards) == [10, 12]


@pytest.mark.anyio
async def test_media_consistency_check_returns_bounded_missing_and_unused_files(
    phase2_collection: str,
) -> None:
    collection = Collection(phase2_collection)
    try:
        deck_id = int(collection.decks.id_for_name("Phase 2") or 0)
        note = collection.new_note(collection.models.current())
        note["Front"] = '<img src="missing.png">'
        note["Back"] = "media"
        collection.add_note(note, deck_id)
    finally:
        collection.close()

    async with AnkiCollectionService(phase2_collection, max_page_size=100) as service:
        await service.store_media("unused.txt", base64.b64encode(b"unused").decode())
        checked = await service.check_media(offset=0, limit=10)

    assert checked["missing"] == ["missing.png"]
    assert checked["unused"] == ["unused.txt"]
    assert checked["missing_total"] == 1
    assert checked["unused_total"] == 1
    assert checked["has_more"] is False
    assert "report" not in checked


def test_legacy_receipts_get_timestamps_and_recent_updates_sort_first(tmp_path: Path) -> None:
    collection_path = tmp_path / "collection.anki2"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    database = sqlite3.connect(state_dir / "idempotency.sqlite")
    database.execute(
        """
        create table mutation_receipts (
            idempotency_key text primary key,
            operation text not null,
            request_hash text not null,
            receipt_json text not null
        )
        """
    )
    receipt = json.dumps({"state": "committed", "remote_synced": True})
    database.execute(
        "insert into mutation_receipts values (?, ?, ?, ?)",
        ("legacy", "legacy_operation", "legacy-hash", receipt),
    )
    database.commit()
    database.close()

    state = PersistentState(collection_path)
    try:
        legacy = state.get_operation("legacy")
        assert legacy is not None
        assert legacy["created_at"]
        assert legacy["updated_at"]
        state.put_receipt("newer", "newer_operation", "newer-hash", {"state": "committed"})
        time.sleep(0.001)
        state.put_receipt(
            "legacy",
            "legacy_operation",
            "legacy-hash",
            {"state": "committed", "remote_synced": True, "result": {"updated": True}},
        )
        operations, _ = state.list_operations(0, 10)
    finally:
        state.close()

    assert operations[0]["idempotency_key"] == "legacy"


def test_full_sync_reconciliation_updates_operation_timestamps(tmp_path: Path) -> None:
    state = PersistentState(tmp_path / "collection.anki2")
    try:
        state.put_receipt(
            "upload",
            "upload_operation",
            "upload-hash",
            {
                "state": "committed",
                "local_committed": True,
                "remote_synced": False,
                "media_synced": True,
            },
        )
        upload_before = state.get_operation("upload")
        assert upload_before is not None
        time.sleep(0.001)
        state.mark_all_remote_synced()
        upload_after = state.get_operation("upload")
        assert upload_after is not None
        assert upload_after["updated_at"] > upload_before["updated_at"]

        state.put_receipt(
            "download",
            "download_operation",
            "download-hash",
            {
                "state": "committed",
                "local_committed": True,
                "remote_synced": False,
                "media_synced": True,
            },
        )
        download_before = state.get_operation("download")
        assert download_before is not None
        time.sleep(0.001)
        state.mark_pending_discarded_by_full_download()
        download_after = state.get_operation("download")
    finally:
        state.close()

    assert download_after is not None
    assert download_after["updated_at"] > download_before["updated_at"]


@pytest.mark.anyio
async def test_operation_status_and_metrics_survive_service_restart(
    phase2_collection: str,
) -> None:
    async with AnkiCollectionService(phase2_collection, max_page_size=100) as service:
        created = await service.coordinated_mutation(
            "anki_decks_create",
            "phase2-operation",
            {"name": "Durable Operation"},
            lambda adapter: adapter.create_deck("Durable Operation"),
        )
        operation = await service.get_operation("phase2-operation")
        listed = await service.list_operations(offset=0, limit=10)
        metrics = await service.metrics()

    async with AnkiCollectionService(phase2_collection, max_page_size=100) as restarted:
        recovered = await restarted.get_operation("phase2-operation")
        recovered_metrics = await restarted.metrics()

    assert operation["idempotency_key"] == "phase2-operation"
    assert operation["operation"] == "anki_decks_create"
    assert operation["receipt"] == created
    assert listed["items"][0]["idempotency_key"] == "phase2-operation"
    assert metrics["mutations"]["total"] == 1
    assert metrics["mutations"]["by_operation"] == {"anki_decks_create": 1}
    assert recovered == operation
    assert recovered_metrics == metrics


@pytest.mark.anyio
async def test_media_sync_waits_for_completion_and_persists_progress(
    phase2_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    statuses = iter(
        [
            MediaSyncStatusResponse(
                active=True, progress=MediaSyncProgress(checked="2", added="1", removed="0")
            ),
            MediaSyncStatusResponse(
                active=False, progress=MediaSyncProgress(checked="3", added="1", removed="1")
            ),
        ]
    )
    monkeypatch.setattr(
        Collection,
        "sync_login",
        lambda self, username, password, endpoint: SyncAuth(hkey="phase2", endpoint=endpoint),
    )
    monkeypatch.setattr(
        Collection,
        "sync_collection",
        lambda self, auth, sync_media: SyncOutput(required=0),
    )
    monkeypatch.setattr("anki._backend.RustBackend.media_sync_status", lambda self: next(statuses))

    async with AnkiCollectionService(phase2_collection, max_page_size=100) as service:
        await service.sync_login("user", "password", None)
        synced = await service.sync(sync_media=True)
        status = await service.status()
        metrics = await service.metrics()

    async with AnkiCollectionService(phase2_collection, max_page_size=100) as restarted:
        recovered = await restarted.status()

    assert synced["media_sync"] == {
        "state": "completed",
        "completed": True,
        "in_progress": False,
        "error": None,
        "checked": "3",
        "added": "1",
        "removed": "1",
    }
    assert status["last_media_sync_at"]
    assert recovered["last_media_sync_at"] == status["last_media_sync_at"]
    assert metrics["sync"]["last_successful_media_sync_at"] == status["last_media_sync_at"]
    assert metrics["sync"]["media_progress"] == {"checked": "3", "added": "1", "removed": "1"}


@pytest.mark.anyio
async def test_media_sync_timeout_preserves_persisted_sync_authentication(
    phase2_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    aborts: list[bool] = []
    monkeypatch.setattr(
        Collection,
        "sync_login",
        lambda self, username, password, endpoint: SyncAuth(hkey="phase2", endpoint=endpoint),
    )
    monkeypatch.setattr(
        Collection,
        "sync_collection",
        lambda self, auth, sync_media: SyncOutput(required=0),
    )
    monkeypatch.setattr(
        "anki._backend.RustBackend.media_sync_status",
        lambda self: MediaSyncStatusResponse(
            active=True, progress=MediaSyncProgress(checked="5", added="2", removed="1")
        ),
    )
    monkeypatch.setattr(Collection, "abort_media_sync", lambda self: aborts.append(True))

    async with AnkiCollectionService(
        phase2_collection, max_page_size=100, sync_timeout_seconds=0.001
    ) as service:
        await service.sync_login("user", "password", None)
        with pytest.raises(TimeoutError, match="media synchronization"):
            await service.sync(sync_media=True)
        status = await service.status()

    assert status["authenticated"] is True
    assert status["media_sync_progress"] == {"checked": "5", "added": "2", "removed": "1"}
    assert aborts == [True]


def _patch_sync(monkeypatch: pytest.MonkeyPatch, required: int = 0) -> None:
    monkeypatch.setattr(
        Collection,
        "sync_login",
        lambda self, username, password, endpoint: SyncAuth(hkey="phase2", endpoint=endpoint),
    )
    monkeypatch.setattr(
        Collection,
        "sync_collection",
        lambda self, auth, sync_media: SyncOutput(required=required),
    )


def _media_status(
    active: bool, checked: str = "0", added: str = "0", removed: str = "0"
) -> MediaSyncStatusResponse:
    return MediaSyncStatusResponse(
        active=active, progress=MediaSyncProgress(checked=checked, added=added, removed=removed)
    )


@pytest.mark.anyio
async def test_media_sync_in_progress_is_returned_and_status_records_completion(
    phase2_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    statuses = iter(
        [
            _media_status(True, "10", "4"),
            _media_status(True, "20", "9"),
            _media_status(False, "30", "12"),
        ]
    )
    _patch_sync(monkeypatch)
    monkeypatch.setattr("anki._backend.RustBackend.media_sync_status", lambda self: next(statuses))
    aborts: list[bool] = []
    monkeypatch.setattr(Collection, "abort_media_sync", lambda self: aborts.append(True))

    async with AnkiCollectionService(
        phase2_collection, max_page_size=100, media_sync_wait_seconds=0
    ) as service:
        await service.sync_login("user", "password", None)
        synced = await service.sync(sync_media=True)
        running = await service.status()
        finished = await service.status()
        # Once finished the backend must not be polled again; the iterator is exhausted.
        settled = await service.status()

    async with AnkiCollectionService(phase2_collection, max_page_size=100) as restarted:
        recovered = await restarted.status()

    assert synced["media_sync"]["state"] == "in_progress"
    assert synced["media_sync"]["completed"] is False
    assert synced["media_sync"]["in_progress"] is True
    assert running["media_sync"]["state"] == "in_progress"
    assert running["last_media_sync_at"] is None
    assert running["media_sync_progress"] == {"checked": "20", "added": "9", "removed": "0"}
    assert finished["media_sync"]["state"] == "completed"
    assert finished["media_sync"]["completed"] is True
    assert finished["last_media_sync_at"]
    assert finished["media_sync_progress"] == {"checked": "30", "added": "12", "removed": "0"}
    assert settled["last_media_sync_at"] == finished["last_media_sync_at"]
    assert recovered["last_media_sync_at"] == finished["last_media_sync_at"]
    assert recovered["media_sync_progress"] == finished["media_sync_progress"]
    assert recovered["media_sync"]["state"] == "idle"
    assert aborts == []


@pytest.mark.anyio
async def test_media_sync_failure_is_recorded_once_by_status(
    phase2_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def media_status(self: object) -> MediaSyncStatusResponse:
        calls.append(1)
        if len(calls) == 1:
            return _media_status(True, "5", "1")
        # Anki raises a failed background media sync's error once, from the first status call.
        raise NetworkError("media transfer failed", None, None, None)

    _patch_sync(monkeypatch)
    monkeypatch.setattr("anki._backend.RustBackend.media_sync_status", media_status)

    async with AnkiCollectionService(
        phase2_collection, max_page_size=100, media_sync_wait_seconds=0
    ) as service:
        await service.sync_login("user", "password", None)
        synced = await service.sync(sync_media=True)
        failed = await service.status()
        later = await service.status()

    assert synced["media_sync"]["state"] == "in_progress"
    assert failed["media_sync"]["state"] == "failed"
    assert failed["media_sync"]["error"] == "NETWORK_ERROR"
    assert failed["media_sync"]["completed"] is False
    assert failed["last_media_sync_at"] is None
    assert failed["authenticated"] is True
    assert later["media_sync"]["state"] == "failed"
    assert len(calls) == 2


@pytest.mark.anyio
async def test_media_sync_lifecycle_is_logged_without_backend_messages(
    phase2_collection: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="anki_mcp.media_sync")
    statuses = iter([_media_status(True, "10", "4"), _media_status(False, "30", "12")])
    _patch_sync(monkeypatch)
    monkeypatch.setattr("anki._backend.RustBackend.media_sync_status", lambda self: next(statuses))

    async with AnkiCollectionService(
        phase2_collection, max_page_size=100, media_sync_wait_seconds=0
    ) as service:
        await service.sync_login("user", "password", None)
        await service.sync(sync_media=True)
        await service.status()

    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith("media sync started source=sync") for m in messages)
    assert any("media sync wait begin budget=0.0s" in m for m in messages)
    assert any(
        "media sync progress checked=10 added=4 removed=0 active=True" in m for m in messages
    )
    assert any("media sync wait end state=in_progress" in m for m in messages)
    completed = [r for r in caplog.records if r.getMessage().startswith("media sync completed")]
    assert len(completed) == 1
    assert completed[0].levelno == logging.INFO
    assert "checked=30 added=12 removed=0" in completed[0].getMessage()


@pytest.mark.anyio
async def test_media_sync_failure_is_logged_as_warning_without_the_backend_message(
    phase2_collection: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="anki_mcp.media_sync")
    calls: list[int] = []

    def media_status(self: object) -> MediaSyncStatusResponse:
        calls.append(1)
        if len(calls) == 1:
            return _media_status(True, "5", "1")
        raise NetworkError("failed contacting https://secret-host.example/", None, None, None)

    _patch_sync(monkeypatch)
    monkeypatch.setattr("anki._backend.RustBackend.media_sync_status", media_status)

    async with AnkiCollectionService(
        phase2_collection, max_page_size=100, media_sync_wait_seconds=0
    ) as service:
        await service.sync_login("user", "password", None)
        await service.sync(sync_media=True)
        await service.status()

    failures = [r for r in caplog.records if r.getMessage().startswith("media sync failed")]
    assert len(failures) == 1
    assert failures[0].levelno == logging.WARNING
    assert "code=NETWORK_ERROR" in failures[0].getMessage()
    assert "detail=NetworkError" in failures[0].getMessage()
    assert "secret-host" not in caplog.text


@pytest.mark.anyio
async def test_media_sync_is_not_started_when_full_sync_is_required(
    phase2_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_poll(self: object) -> MediaSyncStatusResponse:
        raise AssertionError("no media sync was started")

    _patch_sync(monkeypatch, required=2)
    monkeypatch.setattr("anki._backend.RustBackend.media_sync_status", must_not_poll)

    async with AnkiCollectionService(phase2_collection, max_page_size=100) as service:
        await service.sync_login("user", "password", None)
        synced = await service.sync(sync_media=True)
        status = await service.status()

    assert synced["required"] == "FULL_SYNC"
    assert synced["media_sync"]["state"] == "not_started"
    assert synced["media_sync"]["completed"] is False
    assert status["last_media_sync_at"] is None
    assert status["media_sync"]["state"] == "idle"


@pytest.mark.anyio
async def test_progressing_media_sync_is_not_aborted_by_the_stall_timeout(
    phase2_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    polls = iter(range(1, 10_000))
    aborts: list[bool] = []
    _patch_sync(monkeypatch)
    monkeypatch.setattr(
        "anki._backend.RustBackend.media_sync_status",
        lambda self: _media_status(True, str(next(polls))),
    )
    monkeypatch.setattr(Collection, "abort_media_sync", lambda self: aborts.append(True))

    async with AnkiCollectionService(
        phase2_collection,
        max_page_size=100,
        sync_timeout_seconds=0.05,
        media_sync_wait_seconds=0.4,
    ) as service:
        await service.sync_login("user", "password", None)
        synced = await service.sync(sync_media=True)

    assert synced["media_sync"]["state"] == "in_progress"
    assert aborts == []


@pytest.mark.anyio
async def test_stalled_media_sync_is_aborted_and_reported_failed_by_status(
    phase2_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    aborts: list[bool] = []
    _patch_sync(monkeypatch)
    monkeypatch.setattr(
        "anki._backend.RustBackend.media_sync_status",
        lambda self: _media_status(True, "7", "3"),
    )
    monkeypatch.setattr(Collection, "abort_media_sync", lambda self: aborts.append(True))
    monkeypatch.setattr("anki_mcp.collection.MEDIA_SYNC_ABORT_GRACE_SECONDS", 0.01)

    async with AnkiCollectionService(
        phase2_collection,
        max_page_size=100,
        sync_timeout_seconds=0.05,
        media_sync_wait_seconds=0,
    ) as service:
        await service.sync_login("user", "password", None)
        synced = await service.sync(sync_media=True)
        time.sleep(0.1)
        status = await service.status()

    assert synced["media_sync"]["state"] == "in_progress"
    assert status["media_sync"]["state"] == "failed"
    assert status["media_sync"]["error"] == "STALLED"
    assert status["last_media_sync_at"] is None
    assert aborts == [True]


@pytest.mark.anyio
async def test_mutation_receipt_stays_retryable_until_media_sync_finishes(
    phase2_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_active = {"value": True}
    _patch_sync(monkeypatch)
    monkeypatch.setattr(
        "anki._backend.RustBackend.media_sync_status",
        lambda self: _media_status(media_active["value"], "4", "2"),
    )
    replayed = False

    def must_not_replay(_: object) -> dict[str, object]:
        nonlocal replayed
        replayed = True
        raise AssertionError("mutation replayed")

    async with AnkiCollectionService(
        phase2_collection,
        max_page_size=100,
        sync_on_write=True,
        media_sync_wait_seconds=0,
    ) as service:
        await service.sync_login("user", "password", None)
        first = await service.coordinated_mutation(
            operation="anki_media_store",
            idempotency_key="media-in-progress",
            request={"filename": "slow.txt"},
            mutate=lambda adapter: {"created": True},
            sync_media=True,
        )
        media_active["value"] = False
        retried = await service.coordinated_mutation(
            operation="anki_media_store",
            idempotency_key="media-in-progress",
            request={"filename": "slow.txt"},
            mutate=must_not_replay,
            sync_media=True,
        )

    assert first["remote_synced"] is True
    assert first["media_synced"] is False
    assert first["retryable"] is True
    assert retried["media_synced"] is True
    assert retried["retryable"] is False
    assert replayed is False


@pytest.mark.anyio
async def test_mutation_emits_content_free_structured_audit_event(
    phase2_collection: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="anki_mcp.audit")
    async with AnkiCollectionService(phase2_collection, max_page_size=100) as service:
        await service.coordinated_mutation(
            "anki_decks_create",
            "audit-operation",
            {"name": "sensitive deck content"},
            lambda adapter: adapter.create_deck("Audit Deck"),
        )

    event = json.loads(caplog.messages[-1])
    assert event["event"] == "anki_mutation"
    assert event["tool"] == "anki_decks_create"
    assert "idempotency_key" not in event
    assert len(event["operation_id"]) == 16
    assert event["local_committed"] is True
    assert event["remote_synced"] is True
    assert event["duration_ms"] >= 0
    assert "sensitive deck content" not in caplog.text
    assert "audit-operation" not in caplog.text


@pytest.mark.anyio
async def test_backup_can_restore_into_a_disposable_collection(
    phase2_collection: str, tmp_path: Path
) -> None:
    async with AnkiCollectionService(phase2_collection, max_page_size=100) as service:
        backup = await service.create_backup()

    backup_path = Path(backup["path"])
    assert backup_path.is_file()
    with zipfile.ZipFile(backup_path) as archive:
        assert "collection.anki21b" in archive.namelist()

    restored_path = tmp_path / "restored.anki2"
    restored = Collection(str(restored_path))
    try:
        restored.import_anki_package(ImportAnkiPackageRequest(package_path=str(backup_path)))
        assert restored.find_notes('"phase two 0"')
        assert restored.find_notes('"phase two 1"')
    finally:
        restored.close()


def _patch_media_backend(monkeypatch: pytest.MonkeyPatch, backend: dict[str, object]) -> list[bool]:
    """Serve media_sync_status from a mutable dict so tests decide what each poll sees."""
    sync_calls: list[bool] = []

    def sync_collection(self: Collection, auth: SyncAuth, sync_media: bool) -> SyncOutput:
        sync_calls.append(sync_media)
        return SyncOutput(required=0)

    def media_sync_status(self: object) -> MediaSyncStatusResponse:
        error = backend.pop("error", None)
        if isinstance(error, Exception):
            raise error
        return _media_status(bool(backend["active"]), str(backend["checked"]))

    monkeypatch.setattr(
        Collection,
        "sync_login",
        lambda self, username, password, endpoint: SyncAuth(hkey="phase2", endpoint=endpoint),
    )
    monkeypatch.setattr(Collection, "sync_collection", sync_collection)
    monkeypatch.setattr("anki._backend.RustBackend.media_sync_status", media_sync_status)
    return sync_calls


@pytest.mark.anyio
async def test_initial_media_sync_gates_readiness_and_media_tools_until_complete(
    phase2_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend: dict[str, object] = {"active": True, "checked": "10"}
    sync_calls = _patch_media_backend(monkeypatch, backend)

    async with AnkiCollectionService(
        phase2_collection, max_page_size=100, initial_media_sync=True
    ) as service:
        before_login = await service.status()
        await service.list_media(0, 10)
        await service.sync_login("user", "password", None)
        assert await service.advance_initial_media_sync() is True
        running = await service.status()
        with pytest.raises(MediaSyncInProgressError) as guarded:
            await service.list_media(0, 10)
        await service.list_decks(0, 10)
        backend.update(active=False, checked="30")
        assert await service.advance_initial_media_sync() is False
        finished = await service.status()
        await service.list_media(0, 10)

    async with AnkiCollectionService(
        phase2_collection, max_page_size=100, initial_media_sync=True
    ) as restarted:
        assert await restarted.advance_initial_media_sync() is False
        recovered = await restarted.status()

    assert before_login["readiness_reason"] == "authentication_required"
    assert sync_calls == [True]
    assert running["ready"] is False
    assert running["readiness_reason"] == "initial_media_sync_in_progress"
    assert running["media_sync"]["checked"] == "10"
    assert guarded.value.media_sync["state"] == "in_progress"
    assert finished["ready"] is True
    assert finished["readiness_reason"] is None
    assert finished["last_media_sync_at"]
    assert recovered["ready"] is True


@pytest.mark.anyio
async def test_initial_media_sync_retries_after_failure_with_backoff(
    phase2_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend: dict[str, object] = {
        "active": True,
        "checked": "1",
        "error": NetworkError("down", None, None, None),
    }
    sync_calls = _patch_media_backend(monkeypatch, backend)

    async with AnkiCollectionService(
        phase2_collection, max_page_size=100, initial_media_sync=True
    ) as service:
        await service.sync_login("user", "password", None)
        assert await service.advance_initial_media_sync() is True
        failed = await service.status()
        # Still inside the backoff window, so no new attempt is made.
        assert await service.advance_initial_media_sync() is True
        assert sync_calls == [True]

        def expire_backoff(adapter: CollectionAdapter) -> None:
            adapter._initial_media_sync_retry_at = 0  # pyright: ignore[reportPrivateUsage]

        service.executor.submit(expire_backoff).result()
        backend.update(active=False)
        assert await service.advance_initial_media_sync() is False
        recovered = await service.status()

    assert failed["ready"] is False
    assert failed["readiness_reason"] == "initial_media_sync_failed"
    assert failed["media_sync"]["error"] == "NETWORK_ERROR"
    assert sync_calls == [True, True]
    assert recovered["ready"] is True


@pytest.mark.anyio
async def test_initial_media_sync_restarts_after_process_restart_mid_sync(
    phase2_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend: dict[str, object] = {"active": True, "checked": "5"}
    sync_calls = _patch_media_backend(monkeypatch, backend)

    async with AnkiCollectionService(
        phase2_collection, max_page_size=100, initial_media_sync=True
    ) as service:
        await service.sync_login("user", "password", None)
        assert await service.advance_initial_media_sync() is True

    async with AnkiCollectionService(
        phase2_collection, max_page_size=100, initial_media_sync=True
    ) as restarted:
        interrupted = await restarted.status()
        backend.update(active=False, checked="9")
        assert await restarted.advance_initial_media_sync() is False
        finished = await restarted.status()

    assert interrupted["readiness_reason"] == "initial_media_sync_in_progress"
    assert interrupted["media_sync_progress"]["checked"] == "5"
    assert sync_calls == [True, True]
    assert finished["ready"] is True


@pytest.mark.anyio
async def test_initial_media_sync_disabled_leaves_readiness_and_media_alone(
    phase2_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync_calls = _patch_media_backend(monkeypatch, {"active": True, "checked": "0"})

    async with AnkiCollectionService(phase2_collection, max_page_size=100) as service:
        await service.sync_login("user", "password", None)
        assert await service.advance_initial_media_sync() is False
        status = await service.status()
        await service.list_media(0, 10)

    assert sync_calls == []
    assert status["ready"] is True


@pytest.mark.anyio
async def test_media_list_pages_folders_larger_than_scan_bound(phase2_collection: str) -> None:
    async with AnkiCollectionService(
        phase2_collection, max_page_size=2, max_search_scan=1
    ) as service:
        media_dir = Path(phase2_collection).with_suffix(".media")
        for name in ("c.png", "A.png", "b.png"):
            (media_dir / name).write_bytes(b"xy")
        first = await service.list_media(0, 2)
        second = await service.list_media(2, 2)

    assert first["items"] == [
        {"filename": "A.png", "size_bytes": 2},
        {"filename": "b.png", "size_bytes": 2},
    ]
    assert (first["total"], first["has_more"]) == (3, True)
    assert second["items"] == [{"filename": "c.png", "size_bytes": 2}]
    assert second["has_more"] is False


class _FakeEntry:
    def __init__(self, name: str) -> None:
        self.name = name

    def is_file(self, *, follow_symlinks: bool) -> bool:
        return True


class _FakeScandir:
    def __init__(self, names: list[str]) -> None:
        self._entries = [_FakeEntry(name) for name in names]

    def __enter__(self) -> list[_FakeEntry]:
        return self._entries

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.mark.anyio
async def test_media_list_orders_case_variants_stably_and_skips_vanished_files(
    phase2_collection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    listings = iter([["a.png", "A.png", "gone.png"], ["gone.png", "A.png", "a.png"]] * 2)
    monkeypatch.setattr(
        "anki_mcp.collection.os.scandir", lambda _path: _FakeScandir(next(listings))
    )
    async with AnkiCollectionService(phase2_collection, max_page_size=1) as service:
        media_dir = Path(phase2_collection).with_suffix(".media")
        for name in ("A.png", "a.png"):
            (media_dir / name).write_bytes(b"x")
        pages = [await service.list_media(offset, 1) for offset in range(3)]
        repeat = await service.list_media(0, 1)

    assert [page["items"] for page in pages] == [
        [{"filename": "A.png", "size_bytes": 1}],
        [{"filename": "a.png", "size_bytes": 1}],
        [],
    ]
    assert pages[2]["total"] == 3
    assert repeat["items"] == [{"filename": "A.png", "size_bytes": 1}]
