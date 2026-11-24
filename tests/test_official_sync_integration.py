from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from anki.collection import Collection

from anki_mcp.collection import AnkiCollectionService, FullSyncRequiredError


def _free_loopback_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


@pytest.fixture
def official_sync_server(tmp_path: Path) -> Iterator[str]:
    """Run the sync server shipped by the pinned official Anki package."""
    port = _free_loopback_port()
    sync_base = tmp_path / "sync-server"
    sync_base.mkdir()
    environment = {
        **os.environ,
        "SYNC_BASE": str(sync_base),
        "SYNC_HOST": "127.0.0.1",
        "SYNC_PORT": str(port),
        "SYNC_USER1": "phase1-user:phase1-password",
        "RUST_LOG": "anki=warn",
    }
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter/module, test-only environment
        [sys.executable, "-m", "anki.syncserver"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout is not None else ""
            pytest.fail(f"official sync server exited during startup: {output}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        process.terminate()
        pytest.fail("official sync server did not become ready")

    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_complete_phase1_lifecycle_against_official_sync_server(
    official_sync_server: str, tmp_path: Path
) -> None:
    """Prove full-sync gating, write sync, recovery, and idempotency end to end."""
    client_a_path = str(tmp_path / "client-a" / "collection.anki2")
    Path(client_a_path).parent.mkdir()
    collection = Collection(client_a_path)
    try:
        model = collection.models.by_name("Basic")
        assert model is not None
        note_type_id = int(model["id"])
    finally:
        collection.close()

    note_id = 0
    original_receipt: dict[str, object] | None = None

    async def write_from_client_a() -> None:
        nonlocal note_id, original_receipt
        async with AnkiCollectionService(
            client_a_path, max_page_size=100, sync_on_write=True
        ) as service:
            await service.sync_login("phase1-user", "phase1-password", official_sync_server)
            initial = await service.sync(sync_media=False)
            assert initial["required"] == "NO_CHANGES"
            with pytest.raises(FullSyncRequiredError, match="FULL_UPLOAD"):
                await service.coordinated_mutation(
                    operation="anki_notes_create",
                    idempotency_key="official-lifecycle-key",
                    request={"front": "official lifecycle"},
                    mutate=lambda adapter: adapter.create_note(
                        1,
                        note_type_id,
                        {"Front": "official lifecycle", "Back": "first value"},
                        ["phase1"],
                    ),
                )
            blocked = await service.status()
            assert blocked["pending_full_sync"] == "FULL_UPLOAD"
            assert blocked["readiness_reason"] == "full_sync_required"
            assert blocked["pending_mutations"] == 1

            full = await service.full_sync(upload=True)
            assert full["completed"] is True
            assert full["direction"] == "upload"
            original_receipt = await service.coordinated_mutation(
                operation="anki_notes_create",
                idempotency_key="official-lifecycle-key",
                request={"front": "official lifecycle"},
                mutate=lambda adapter: (_ for _ in ()).throw(
                    AssertionError("local mutation was replayed")
                ),
            )
            assert original_receipt["remote_synced"] is True
            note_id = int(original_receipt["result"]["note_id"])  # type: ignore[index]

    asyncio.run(write_from_client_a())

    async def verify_restart_and_second_client() -> None:
        async with AnkiCollectionService(
            client_a_path, max_page_size=100, sync_on_write=True
        ) as restarted:
            status = await restarted.status()
            assert status["authenticated"] is True
            assert status["pending_mutations"] == 0
            repeated = await restarted.coordinated_mutation(
                operation="anki_notes_create",
                idempotency_key="official-lifecycle-key",
                request={"front": "official lifecycle"},
                mutate=lambda adapter: (_ for _ in ()).throw(
                    AssertionError("local mutation was replayed after restart")
                ),
            )
            assert repeated == original_receipt

        client_b_path = str(tmp_path / "client-b" / "collection.anki2")
        Path(client_b_path).parent.mkdir()
        Collection(client_b_path).close()
        async with AnkiCollectionService(
            client_b_path, max_page_size=100, sync_on_write=True
        ) as second_client:
            await second_client.sync_login("phase1-user", "phase1-password", official_sync_server)
            required = await second_client.sync(sync_media=False)
            assert required["required"] == "FULL_DOWNLOAD"
            await second_client.full_sync(upload=False)
            downloaded = await second_client.get_note(note_id)
            assert downloaded["id"] == note_id

            updated = await second_client.coordinated_mutation(
                operation="anki_notes_update_fields",
                idempotency_key="official-update-key",
                request={"note_id": note_id, "fields": {"Back": "second value"}},
                mutate=lambda adapter: adapter.update_note_fields(
                    note_id, {"Back": "second value"}
                ),
            )
            assert updated["local_committed"] is True
            assert updated["remote_synced"] is True

        async with AnkiCollectionService(client_a_path, max_page_size=100) as first_client:
            refreshed = await first_client.coordinated_read(
                lambda adapter: adapter.get_note(note_id), sync_before=True
            )
        fields = {item["name"]: item["value"] for item in refreshed["fields"]}
        assert fields["Back"] == "second value"

    asyncio.run(verify_restart_and_second_client())


def _seed_server_with_media(server_url: str, collection_path: str, file_count: int) -> None:
    """Upload a collection holding many media files, using Anki's own client API directly."""
    Path(collection_path).parent.mkdir(parents=True, exist_ok=True)
    collection = Collection(collection_path)
    try:
        note = collection.new_note(collection.models.current())
        note["Front"] = "media seed"
        note["Back"] = "answer"
        collection.add_note(note, collection.decks.id("Media Seed"))
        for index in range(file_count):
            collection.media.write_data(
                f"seed-{index:05d}.txt", f"media file {index} ".encode() * 16
            )
        auth = collection.sync_login("phase1-user", "phase1-password", server_url)
        output = collection.sync_collection(auth, sync_media=False)
        assert output.required in {2, 4}, output.required
        collection.full_upload_or_download(
            auth=auth, server_usn=output.server_media_usn, upload=True
        )
        deadline = time.monotonic() + 120
        while collection.media_sync_status().active:
            assert time.monotonic() < deadline, "seeding media sync did not finish"
            time.sleep(0.05)
    finally:
        collection.close()


def test_large_media_sync_runs_in_background_and_status_stays_responsive(
    official_sync_server: str, tmp_path: Path
) -> None:
    """A media sync far larger than any single request must finish without blocking callers."""
    file_count = 1500
    _seed_server_with_media(
        official_sync_server, str(tmp_path / "seed" / "collection.anki2"), file_count
    )
    client_path = str(tmp_path / "client" / "collection.anki2")
    Path(client_path).parent.mkdir()
    Collection(client_path).close()

    async def sync_from_empty_client() -> tuple[list[float], set[str], dict[str, object]]:
        async with AnkiCollectionService(
            client_path, max_page_size=100, media_sync_wait_seconds=0
        ) as service:
            await service.sync_login("phase1-user", "phase1-password", official_sync_server)
            first = await service.sync(sync_media=True)
            # The server has a collection this client lacks, so a full download comes first.
            assert first["required"] == "FULL_DOWNLOAD"
            assert first["media_sync"]["state"] == "not_started"
            await service.full_sync(upload=False)

            durations: list[float] = []
            states: set[str] = set()
            deadline = time.monotonic() + 120
            while True:
                began = time.monotonic()
                status = await service.status()
                durations.append(time.monotonic() - began)
                states.add(status["media_sync"]["state"])
                if status["media_sync"]["state"] != "in_progress":
                    break
                assert time.monotonic() < deadline, "media sync did not finish"
                await asyncio.sleep(0.02)
            return durations, states, status

    durations, states, status = asyncio.run(sync_from_empty_client())

    assert status["media_sync"]["state"] == "completed", status["media_sync"]
    assert status["last_media_sync_at"]
    assert status["media_sync"]["error"] is None
    # status never waits on the transfer, however long the media sync runs.
    assert max(durations) < 2, max(durations)
    downloaded = {path.name for path in (Path(client_path).parent / "collection.media").iterdir()}
    assert {f"seed-{index:05d}.txt" for index in range(file_count)} <= downloaded

    async def sync_again() -> dict[str, object]:
        async with AnkiCollectionService(client_path, max_page_size=100) as service:
            return await service.sync(sync_media=True)

    again = asyncio.run(sync_again())
    assert again["media_sync"]["state"] == "completed"


def test_initial_media_sync_completes_after_bootstrap_against_official_server(
    official_sync_server: str, tmp_path: Path
) -> None:
    """A bootstrapped empty client fetches all remote media with no media tool call."""
    file_count = 1500
    _seed_server_with_media(
        official_sync_server, str(tmp_path / "seed" / "collection.anki2"), file_count
    )
    client_path = str(tmp_path / "client" / "collection.anki2")
    Path(client_path).parent.mkdir()
    Collection(client_path).close()

    async def bootstrap_and_drive() -> tuple[set[str | None], dict[str, Any]]:
        async with AnkiCollectionService(
            client_path, max_page_size=100, initial_media_sync=True
        ) as service:
            await service.bootstrap(
                "download_if_empty", "phase1-user", "phase1-password", official_sync_server
            )
            reasons: set[str | None] = set()
            deadline = time.monotonic() + 120
            while await service.advance_initial_media_sync():
                reasons.add((await service.status())["readiness_reason"])
                assert time.monotonic() < deadline, "initial media sync did not finish"
                await asyncio.sleep(0.05)
            return reasons, await service.status()

    reasons, status = asyncio.run(bootstrap_and_drive())

    assert "initial_media_sync_in_progress" in reasons
    assert status["ready"] is True
    assert status["last_media_sync_at"]
    downloaded = {path.name for path in (Path(client_path).parent / "collection.media").iterdir()}
    assert {f"seed-{index:05d}.txt" for index in range(file_count)} <= downloaded


def test_structural_change_from_another_device_downloads_only_when_nothing_is_unsynced(
    official_sync_server: str, tmp_path: Path
) -> None:
    desktop_path = tmp_path / "desktop" / "collection.anki2"
    desktop_path.parent.mkdir()
    desktop = Collection(str(desktop_path))
    note = desktop.new_note(desktop.models.by_name("Basic"))
    note["Front"] = "front"
    note["Back"] = "back"
    desktop.add_note(note, desktop.decks.id("Desktop"))
    auth = desktop.sync_login("phase1-user", "phase1-password", official_sync_server)

    def desktop_uploads(change: Any) -> None:
        model = desktop.models.by_name("Basic")
        change(model)
        desktop.models.update_dict(model)
        output = desktop.sync_collection(auth, sync_media=False)
        assert output.required in {2, 4}, output.required
        desktop.full_upload_or_download(auth=auth, server_usn=None, upload=True)

    def add_reverse(model: Any) -> None:
        template = desktop.models.new_template("Reverse")
        template["qfmt"] = "{{Back}}"
        template["afmt"] = "{{Front}}"
        desktop.models.add_template(model, template)

    output = desktop.sync_collection(auth, sync_media=False)
    desktop.full_upload_or_download(auth=auth, server_usn=output.server_media_usn, upload=True)

    server_path = tmp_path / "server" / "collection.anki2"
    server_path.parent.mkdir()
    Collection(str(server_path)).close()

    async def scenario() -> None:
        async with AnkiCollectionService(
            str(server_path), max_page_size=100, sync_on_write=False
        ) as server:
            await server.sync_login("phase1-user", "phase1-password", official_sync_server)
            first = await server.sync(sync_media=False)
            assert first["required"] == "FULL_DOWNLOAD"
            assert first["full_download"] is None
            await server.full_sync(upload=False)

            desktop_uploads(add_reverse)
            clean = await server.sync(sync_media=False)
            assert clean["required"] == "NO_CHANGES"
            assert clean["full_download"]["direction"] == "download"
            basic = await server.coordinated_read(
                lambda adapter: adapter.collection.models.by_name("Basic")
            )
            assert len(basic["tmpls"]) == 2

            await server.coordinated_mutation(
                operation="anki_notes_create",
                idempotency_key="unsynced-on-server",
                request={"front": "server only"},
                mutate=lambda adapter: adapter.create_note(
                    1, int(basic["id"]), {"Front": "server only", "Back": "x"}, []
                ),
            )
            desktop_uploads(lambda model: desktop.models.remove_template(model, model["tmpls"][1]))
            dirty = await server.sync(sync_media=False)
            assert dirty["required"] == "FULL_SYNC"
            assert dirty["full_download"] is None
            kept = await server.coordinated_read(
                lambda adapter: adapter.collection.find_notes('"front:server only"')
            )
            assert len(kept) == 1

    try:
        asyncio.run(scenario())
    finally:
        desktop.close()
