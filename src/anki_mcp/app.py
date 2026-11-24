from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Generic, Literal, NoReturn, TypeVar, cast
from uuid import uuid4

from anki.errors import NetworkError, SyncError, SyncErrorKind
from anyio import move_on_after
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import McpError
from mcp.types import ClientCapabilities, ElicitationCapability, ToolAnnotations
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from anki_mcp.auth import BearerAuthMiddleware, RequestBodyLimitMiddleware
from anki_mcp.collection import (
    AnkiCollectionService,
    BackupFailedError,
    CollectionAdapter,
    DuplicateNoteError,
    FullSyncRequiredError,
    IdempotencyConflictError,
    MediaSyncFailedError,
    MediaSyncInProgressError,
    ResourceLimitError,
    SyncLoginRequiredError,
)
from anki_mcp.config import Settings
from anki_mcp.guard import ConfirmationRegistry
from anki_mcp.oauth import BuiltinOAuthProvider

T = TypeVar("T")
LOGGER = logging.getLogger(__name__)
_OAUTH_MAX_REQUEST_BYTES = 16_384
_INITIAL_MEDIA_SYNC_POLL_SECONDS = 2.0
# /health/ready stays a process-level check: a large first media sync must not mark it unhealthy.
_PROCESS_READY_REASONS = {None, "initial_media_sync_in_progress", "initial_media_sync_failed"}
Offset = StrictInt
PageLimit = StrictInt
StableId = Annotated[StrictInt, Field(gt=0)]
Confirmation = StrictBool
SyncMedia = StrictBool
DeckName = Annotated[StrictStr, Field(min_length=1, max_length=512)]
SearchQuery = Annotated[StrictStr, Field(max_length=4096)]
CardText = Annotated[StrictStr, Field(max_length=262_144)]
IdempotencyKey = Annotated[StrictStr, Field(min_length=1, max_length=128)]
Tag = Annotated[StrictStr, Field(min_length=1, max_length=512)]
ResourceName = Annotated[StrictStr, Field(min_length=1, max_length=512)]
MediaFilename = Annotated[StrictStr, Field(min_length=1, max_length=255)]
MediaContent = Annotated[StrictStr, Field(min_length=1, max_length=22_369_624)]
StableIds = Annotated[list[StableId], Field(min_length=1, max_length=500)]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
PositiveInt = Annotated[StrictInt, Field(gt=0)]
CardFlag = Annotated[StrictInt, Field(ge=0, le=7)]
DailyLimit = Annotated[StrictInt, Field(ge=0, le=999_999)]
Retention = Annotated[StrictFloat, Field(ge=0.7, le=0.99)]
Tags = Annotated[list[Tag], Field(max_length=100)]
NoteFields = dict[Annotated[StrictStr, Field(min_length=1, max_length=512)], CardText]
ConfirmationToken = Annotated[StrictStr, Field(min_length=1, max_length=256)]
SchemaChangeOperation = Literal["create", "update", "fields_update", "templates_update"]
NoteTypeKind = Literal["standard", "cloze"]
ReviewRating = Annotated[StrictInt, Field(ge=1, le=4)]
AnswerSeconds = Annotated[StrictInt, Field(ge=0, le=86_400)]
ReviewOrder = Literal["newest", "oldest"]
ReviewDays = Literal[0, 30, 90, 365]
CardGetSection = Literal["review_summary", "fsrs"]
CardGetSections = Annotated[tuple[CardGetSection, ...], Field(max_length=2)]
ReviewEventField = Literal[
    "review_kind", "rating_label", "intervals", "answer_time", "ease", "memory_state"
]
ReviewEventFields = Annotated[tuple[ReviewEventField, ...], Field(max_length=6)]
ReviewStatsSection = Literal[
    "buttons",
    "card_counts",
    "hours",
    "today",
    "eases",
    "intervals",
    "future_due",
    "added",
    "reviews",
    "rollover_hour",
    "difficulty",
    "retrievability",
    "stability",
    "true_retention",
    "fsrs",
]
ReviewStatsSections = Annotated[tuple[ReviewStatsSection, ...], Field(max_length=15)]
DeckOptionSection = Literal["counts", "parents", "global_settings"]
DeckOptionSections = Annotated[tuple[DeckOptionSection, ...], Field(max_length=3)]
DeckPresetSection = Literal[
    "learning",
    "new_cards",
    "reviews",
    "lapses",
    "burying",
    "display_audio",
    "fsrs",
    "easy_days",
]
DeckPresetSections = Annotated[tuple[DeckPresetSection, ...], Field(max_length=8)]
DailyDeckLimitField = Literal["new_cards_per_day", "reviews_per_day"]
DailyDeckLimitFields = Annotated[tuple[DailyDeckLimitField, ...], Field(max_length=2)]
ThisDeckLimitField = Literal["new_cards_per_day", "reviews_per_day", "desired_retention"]
ThisDeckLimitFields = Annotated[tuple[ThisDeckLimitField, ...], Field(max_length=3)]
NonNegativeFloat = Annotated[StrictFloat, Field(ge=0)]
PositiveFloat = Annotated[StrictFloat, Field(gt=0)]
Steps = Annotated[list[NonNegativeFloat], Field(max_length=100)]
FsrsParameters = Annotated[list[StrictFloat], Field(max_length=30)]
FiniteFloat = Annotated[StrictFloat, Field(allow_inf_nan=False)]
FsrsRescheduleParameters = Annotated[
    list[FiniteFloat],
    Field(
        min_length=21,
        max_length=21,
        description="Finite 21-value FSRS-6 parameter vector.",
    ),
]
FsrsSimulationMode = Literal["review", "workload", "optimal_retention"]
FsrsSimulationDays = Annotated[StrictInt, Field(ge=1, le=730)]
FsrsSimulationDeckSize = Annotated[
    StrictInt,
    Field(
        ge=1,
        le=1_000_000,
        description="Hypothetical simulated deck size; it is not inferred from the collection.",
    ),
]
DeckPresetId = Annotated[
    StrictInt,
    Field(gt=0, description="Shared deck preset ID from anki_deck_presets_list or deck options."),
]
EasyDaysPercentages = Annotated[list[NonNegativeFloat], Field(min_length=7, max_length=7)]
NewCardInsertOrder = Literal["NEW_CARD_INSERT_ORDER_DUE", "NEW_CARD_INSERT_ORDER_RANDOM"]
NewCardGatherPriority = Literal[
    "NEW_CARD_GATHER_PRIORITY_DECK",
    "NEW_CARD_GATHER_PRIORITY_DECK_THEN_RANDOM_NOTES",
    "NEW_CARD_GATHER_PRIORITY_LOWEST_POSITION",
    "NEW_CARD_GATHER_PRIORITY_HIGHEST_POSITION",
    "NEW_CARD_GATHER_PRIORITY_RANDOM_NOTES",
    "NEW_CARD_GATHER_PRIORITY_RANDOM_CARDS",
]
NewCardSortOrder = Literal[
    "NEW_CARD_SORT_ORDER_TEMPLATE",
    "NEW_CARD_SORT_ORDER_NO_SORT",
    "NEW_CARD_SORT_ORDER_TEMPLATE_THEN_RANDOM",
    "NEW_CARD_SORT_ORDER_RANDOM_NOTE_THEN_TEMPLATE",
    "NEW_CARD_SORT_ORDER_RANDOM_CARD",
]
ReviewMix = Literal[
    "REVIEW_MIX_MIX_WITH_REVIEWS", "REVIEW_MIX_AFTER_REVIEWS", "REVIEW_MIX_BEFORE_REVIEWS"
]
ReviewCardOrder = Literal[
    "REVIEW_CARD_ORDER_DAY",
    "REVIEW_CARD_ORDER_DAY_THEN_DECK",
    "REVIEW_CARD_ORDER_DECK_THEN_DAY",
    "REVIEW_CARD_ORDER_INTERVALS_ASCENDING",
    "REVIEW_CARD_ORDER_INTERVALS_DESCENDING",
    "REVIEW_CARD_ORDER_EASE_ASCENDING",
    "REVIEW_CARD_ORDER_EASE_DESCENDING",
    "REVIEW_CARD_ORDER_RETRIEVABILITY_ASCENDING",
    "REVIEW_CARD_ORDER_RETRIEVABILITY_DESCENDING",
    "REVIEW_CARD_ORDER_RELATIVE_OVERDUENESS",
    "REVIEW_CARD_ORDER_RANDOM",
    "REVIEW_CARD_ORDER_ADDED",
    "REVIEW_CARD_ORDER_REVERSE_ADDED",
]
LeechAction = Literal["LEECH_ACTION_SUSPEND", "LEECH_ACTION_TAG_ONLY"]
QuestionAction = Literal["QUESTION_ACTION_SHOW_ANSWER", "QUESTION_ACTION_SHOW_REMINDER"]
AnswerAction = Literal[
    "ANSWER_ACTION_BURY_CARD",
    "ANSWER_ACTION_ANSWER_AGAIN",
    "ANSWER_ACTION_ANSWER_GOOD",
    "ANSWER_ACTION_ANSWER_HARD",
    "ANSWER_ACTION_SHOW_REMINDER",
]


_SYNC_META_KEY = "anki-mcp/sync"
_FULL_SYNC_WARNING = (
    "This forces a full sync. Sync every other device that has Anki changes first, "
    "or those changes will be lost."
)


def _manual_note_type_steps(
    operation: str, note_type: dict[str, Any], mappings: list[dict[str, Any]]
) -> dict[str, Any]:
    """Describe a structural note-type change so a person can make it in Anki desktop."""
    templates = operation == "anki_templates_update"
    kind = "templates" if templates else "fields"
    current = {int(item["ordinal"]): str(item["name"]) for item in note_type[kind]}
    target = []
    for position, mapping in enumerate(mappings, start=1):
        source = mapping["source_ordinal"]
        changes = []
        if source is None:
            changes.append("add")
        elif source + 1 != position:
            changes.append(f"move from position {source + 1}")
        if source is not None and mapping["name"] != current[source]:
            changes.append(f"rename from {current[source]}")
        entry: dict[str, Any] = {
            "position": position,
            "name": mapping["name"],
            "change": ", ".join(changes) or "keep",
        }
        if templates:
            entry["front"] = mapping["question_format"]
            entry["back"] = mapping["answer_format"]
        target.append(entry)
    kept = {mapping["source_ordinal"] for mapping in mappings}
    screen, tools = (
        (
            "Cards",
            "Options > Add Card Type, Remove Card Type and Reposition, pasting each "
            "front and back template",
        )
        if templates
        else ("Fields", "Add, Delete, Rename and Reposition")
    )
    return {
        "note_type": note_type["name"],
        kind: target,
        "remove": [name for ordinal, name in sorted(current.items()) if ordinal not in kept],
        "steps": [
            "Sync every device that has Anki changes, so none are lost.",
            f"In Anki desktop open Tools > Manage Note Types, select {note_type['name']}, "
            f"then click {screen}.",
            f"Use {tools} until they match the list above, and remove the ones under 'remove'.",
            "Anki warns that this needs a one-way sync. Accept, then sync and choose Upload.",
            "This server downloads the change on its next sync.",
        ],
    }


class _HumanConfirmation(BaseModel):
    confirm: bool = Field(description="Apply this change")


async def _ask_person(context: Context, message: str, timeout: float) -> bool | None:
    """Return the person's answer, or None when the client cannot ask; silence is a no."""
    if not context.session.check_client_capability(
        ClientCapabilities(elicitation=ElicitationCapability())
    ):
        return None
    with move_on_after(timeout):
        try:
            answer = await context.elicit(message, _HumanConfirmation)
        except McpError:
            return False
        return answer.action == "accept" and answer.data is not None and answer.data.confirm
    return False


class NoteCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    deck_id: StableId
    note_type_id: StableId
    fields: NoteFields
    tags: Tags = Field(default_factory=list)


class NoteTypeTemplateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: ResourceName
    question_format: CardText
    answer_format: CardText


class NoteTypeFieldMappingInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: ResourceName
    source_ordinal: NonNegativeInt | None = None


class NoteTypeTemplateMappingInput(NoteTypeTemplateInput):
    source_ordinal: NonNegativeInt | None = None


class DeckTodayLimitPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    new_cards_per_day: DailyLimit | None = None
    reviews_per_day: DailyLimit | None = None


class DeckThisLimitPatch(DeckTodayLimitPatch):
    desired_retention: Retention | None = None


class DeckTodayLimitUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    scope: Literal["today"]
    values: DeckTodayLimitPatch | None = None
    clear_fields: DailyDeckLimitFields = ()


class DeckThisLimitUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    scope: Literal["this_deck"]
    values: DeckThisLimitPatch | None = None
    clear_fields: ThisDeckLimitFields = ()


DeckLimitUpdate = Annotated[
    DeckTodayLimitUpdate | DeckThisLimitUpdate,
    Field(discriminator="scope"),
]


class DeckPresetPatch(BaseModel):
    """Typed patch over every user-facing field in Anki's deck preset config."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    learn_steps: Steps | None = None
    relearn_steps: Steps | None = None
    fsrs_params_4: FsrsParameters | None = None
    fsrs_params_5: FsrsParameters | None = None
    fsrs_params_6: FsrsParameters | None = None
    new_per_day: DailyLimit | None = None
    reviews_per_day: DailyLimit | None = None
    new_per_day_minimum: DailyLimit | None = None
    initial_ease: PositiveFloat | None = None
    easy_multiplier: PositiveFloat | None = None
    hard_multiplier: PositiveFloat | None = None
    lapse_multiplier: NonNegativeFloat | None = None
    interval_multiplier: PositiveFloat | None = None
    maximum_review_interval: PositiveInt | None = None
    minimum_lapse_interval: PositiveInt | None = None
    graduating_interval_good: PositiveInt | None = None
    graduating_interval_easy: PositiveInt | None = None
    new_card_insert_order: NewCardInsertOrder | None = None
    new_card_gather_priority: NewCardGatherPriority | None = None
    new_card_sort_order: NewCardSortOrder | None = None
    new_mix: ReviewMix | None = None
    review_order: ReviewCardOrder | None = None
    interday_learning_mix: ReviewMix | None = None
    leech_action: LeechAction | None = None
    leech_threshold: PositiveInt | None = None
    disable_autoplay: StrictBool | None = None
    cap_answer_time_to_secs: NonNegativeInt | None = None
    show_timer: StrictBool | None = None
    stop_timer_on_answer: StrictBool | None = None
    seconds_to_show_question: NonNegativeFloat | None = None
    seconds_to_show_answer: NonNegativeFloat | None = None
    question_action: QuestionAction | None = None
    answer_action: AnswerAction | None = None
    wait_for_audio: StrictBool | None = None
    skip_question_when_replaying_answer: StrictBool | None = None
    bury_new: StrictBool | None = None
    bury_reviews: StrictBool | None = None
    bury_interday_learning: StrictBool | None = None
    desired_retention: Retention | None = None
    ignore_revlogs_before_date: Annotated[StrictStr, Field(max_length=32)] | None = None
    easy_days_percentages: EasyDaysPercentages | None = None
    historical_retention: Retention | None = None
    param_search: SearchQuery | None = None


FieldMappings = Annotated[list[NoteTypeFieldMappingInput], Field(min_length=1, max_length=1000)]
TemplateMappings = Annotated[
    list[NoteTypeTemplateMappingInput], Field(min_length=1, max_length=1000)
]


class ResponseTooLargeError(RuntimeError):
    """Raised when a tool result exceeds the configured serialized response budget."""


class ConfirmationRequiredError(ValueError):
    """Raised when a guarded operation lacks a valid preview token."""


class FsrsAffectedDeck(BaseModel):
    id: int
    name: str


class FsrsOptimizationImpact(BaseModel):
    config_id: int
    search: str
    training_items: int
    health_check_requested: bool
    health_check_passed: bool
    current_parameters: list[float]
    candidate_parameters: list[float]
    affected_decks: int
    affected_deck_ids: list[int]
    affected_decks_detail: list[FsrsAffectedDeck]
    state_fingerprint: str


class FsrsOptimizationPreviewResult(BaseModel):
    operation: Literal["anki_fsrs_optimize_apply"]
    impact: FsrsOptimizationImpact
    confirmation_token: str
    expires_in_seconds: int


class FsrsOptimizationApplied(BaseModel):
    config_id: int
    optimized: Literal[True]
    search: str
    training_items: int
    health_check_requested: bool
    health_check_passed: bool
    previous_parameters: list[float]
    parameters: list[float]
    affected_decks: int
    affected_deck_ids: list[int]
    affected_decks_detail: list[FsrsAffectedDeck]


class FsrsMutationReceipt(BaseModel, Generic[T]):  # noqa: UP046
    idempotency_key: str
    state: Literal["outcome_unknown", "committed", "discarded_by_full_download"]
    local_committed: bool | None
    remote_synced: bool
    media_synced: bool | None
    retryable: bool
    result: T | None


class FsrsRescheduleImpact(BaseModel):
    config_id: int
    decks: int
    cards: int
    desired_retention: float
    parameters_changed: bool
    state_fingerprint: str


class FsrsReschedulePreviewResult(BaseModel):
    operation: Literal["anki_fsrs_reschedule"]
    impact: FsrsRescheduleImpact
    confirmation_token: str
    expires_in_seconds: int


class BackupReceipt(BaseModel):
    created: bool
    path: str


class FsrsRescheduleApplied(BaseModel):
    config_id: int
    rescheduled: Literal[True]
    cards: int
    decks: int
    desired_retention: float
    parameters_changed: bool
    backup: BackupReceipt


class FsrsReviewSummary(BaseModel):
    total_reviews: int
    total_new_cards: int
    total_time_seconds: float
    final_knowledge_acquisition: float


class FsrsDailyReview(BaseModel):
    day: int
    reviews: int
    new_cards: int
    time_seconds: float
    knowledge_acquisition: float


class FsrsSimulationBase(BaseModel):
    config_id: int
    search: str
    days_to_simulate: int
    deck_size: int


class FsrsReviewSimulationResult(FsrsSimulationBase):
    mode: Literal["review"]
    summary: FsrsReviewSummary
    daily: list[FsrsDailyReview] | None = None


class FsrsWorkloadRetention(BaseModel):
    cost_seconds: dict[str, float]
    memorized_cards: dict[str, float]
    review_count: dict[str, int]


class FsrsWorkloadSimulationResult(FsrsSimulationBase):
    mode: Literal["workload"]
    retention: FsrsWorkloadRetention
    reviewless_end_memorized: float


class FsrsOptimalRetentionSimulationResult(FsrsSimulationBase):
    mode: Literal["optimal_retention"]
    optimal_retention: float


FsrsSimulationResult = Annotated[
    FsrsReviewSimulationResult
    | FsrsWorkloadSimulationResult
    | FsrsOptimalRetentionSimulationResult,
    Field(discriminator="mode"),
]


def create_app(settings: Settings) -> ASGIApp:
    """Create the complete ASGI application and MCP registry."""

    service = AnkiCollectionService(
        settings.collection_path,
        settings.max_page_size,
        settings.max_search_scan,
        settings.max_rendered_field_bytes,
        settings.max_card_fields,
        settings.max_batch_size,
        settings.max_media_bytes,
        settings.sync_timeout_seconds,
        settings.sync_on_read,
        settings.sync_on_write,
        settings.media_sync_wait_seconds,
        settings.initial_media_sync,
    )
    oauth_provider: BuiltinOAuthProvider | None = None
    auth_settings: AuthSettings | None = None
    if settings.auth_mode == "builtin":
        oauth_provider = BuiltinOAuthProvider(settings)
        auth_settings = AuthSettings(
            issuer_url=AnyHttpUrl(settings.public_url),
            resource_server_url=AnyHttpUrl(settings.public_url + settings.mcp_path),
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=oauth_provider.scopes,
                default_scopes=oauth_provider.scopes,
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
    mcp = FastMCP(
        "anki-mcp",
        instructions="Authenticated sync and deck/card CRUD for one Anki collection.",
        streamable_http_path=settings.mcp_path,
        # The SDK drops server-to-client requests in JSON response mode, so elicitation needs SSE.
        # Full-sync tools always ask the person, so registering them also needs SSE.
        json_response=settings.confirmation_mode == "token" and not settings.allow_full_sync,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=settings.allowed_origins,
        ),
        auth_server_provider=oauth_provider,
        auth=auth_settings,
    )
    registered_tool_names: list[str] = []
    conditional_sync_tool_names: set[str] = set()
    confirmations = ConfirmationRegistry(settings.confirmation_ttl_seconds)

    def scoped_tool(
        name: str,
        scope: str,
        *,
        enabled: bool = True,
        destructive: bool = False,
        sync: Literal["incremental", "conditional", "full"] = "incremental",
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(function: Callable[..., Any]) -> Callable[..., Any]:
            if scope in settings.scopes and enabled:
                read_only = scope == "read" or name.endswith("_preview")
                annotations = ToolAnnotations(
                    readOnlyHint=read_only,
                    destructiveHint=destructive or sync != "incremental",
                    idempotentHint=read_only,
                )
                meta = {_SYNC_META_KEY: "none" if read_only else sync}
                mcp.tool(name=name, annotations=annotations, meta=meta)(function)
                if sync == "conditional":
                    conditional_sync_tool_names.add(name)
                registered_tool_names.append(name)
            return function

        return register

    def raise_tool_error(
        code: str,
        message: str,
        cause: Exception,
        *,
        log_cause: bool = False,
        details: dict[str, Any] | None = None,
    ) -> NoReturn:
        correlation_id = str(uuid4())
        payload: dict[str, Any] = {
            "code": code,
            "message": message,
            "correlation_id": correlation_id,
        }
        if details is not None:
            payload["details"] = details
        if log_cause:
            LOGGER.error(
                "tool failure code=%s correlation_id=%s exception_type=%s",
                code,
                correlation_id,
                type(cause).__name__,
                exc_info=(type(cause), cause, cause.__traceback__),
            )
        raise ToolError(json.dumps(payload, separators=(",", ":"))) from cause

    def encoded_size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def fit_page(result: dict[str, Any]) -> dict[str, Any]:
        """Drop trailing items so the page fits MCP_MAX_RESPONSE_BYTES, keeping at least one."""
        items = result["items"]
        size = encoded_size({**result, "items": []})
        kept = 0
        for item in items:
            # +1 for the separating comma; overcounts by one, which errs on the safe side.
            size += encoded_size(item) + 1
            if kept and size > settings.max_response_bytes:
                break
            kept += 1
        if kept < len(items):
            # limit shrinks too, so clients that page by offset + limit stay correct.
            result.update(items=items[:kept], limit=kept, has_more=True)
        return result

    def note_type_update_request(
        note_type_id: int,
        name: str | None,
        fields: list[str] | None,
        templates: list[dict[str, str]] | None,
        css: str | None,
    ) -> dict[str, Any]:
        changes = {"name": name, "fields": fields, "templates": templates, "css": css}
        sent = {key: value for key, value in changes.items() if value is not None}
        if not sent:
            cause = ValueError("note type update requires name, fields, templates, or css")
            raise_tool_error("INVALID_ARGUMENT", str(cause), cause)
        return {"note_type_id": note_type_id, **sent}

    async def execute(
        operation: Awaitable[T], receipt: Callable[[T], dict[str, Any]] | None = None
    ) -> T | dict[str, Any]:
        try:
            raw_result = await operation
            result = receipt(raw_result) if receipt is not None else raw_result
            if encoded_size(result) > settings.max_response_bytes:
                raise ResponseTooLargeError("tool response exceeds MCP_MAX_RESPONSE_BYTES")
            return result
        except DuplicateNoteError as exc:
            raise_tool_error("DUPLICATE_NOTE", str(exc), exc)
        except IdempotencyConflictError as exc:
            raise_tool_error("CONFLICT", str(exc), exc)
        except FullSyncRequiredError as exc:
            raise_tool_error("FULL_SYNC_REQUIRED", str(exc), exc)
        except LookupError as exc:
            raise_tool_error("NOT_FOUND", str(exc), exc)
        except ConfirmationRequiredError as exc:
            raise_tool_error("DESTRUCTIVE_CONFIRMATION_REQUIRED", str(exc), exc)
        except ResourceLimitError as exc:
            raise_tool_error("RESOURCE_LIMIT_EXCEEDED", str(exc), exc)
        except ValueError as exc:
            raise_tool_error("INVALID_ARGUMENT", str(exc), exc)
        except SyncLoginRequiredError as exc:
            raise_tool_error("AUTHENTICATION_FAILED", str(exc), exc)
        except MediaSyncFailedError as exc:
            raise_tool_error("MEDIA_SYNC_FAILED", str(exc), exc)
        except MediaSyncInProgressError as exc:
            raise_tool_error(
                "MEDIA_SYNC_IN_PROGRESS", str(exc), exc, details={"media_sync": exc.media_sync}
            )
        except BackupFailedError as exc:
            raise_tool_error(
                "BACKUP_FAILED",
                "required collection backup could not be created",
                exc,
                log_cause=True,
            )
        except ResponseTooLargeError as exc:
            raise_tool_error("RESPONSE_TOO_LARGE", str(exc), exc)
        except NetworkError as exc:
            raise_tool_error("NETWORK_ERROR", "remote sync network request failed", exc)
        except SyncError as exc:
            code = "AUTHENTICATION_FAILED" if exc.kind == SyncErrorKind.AUTH else "SYNC_ERROR"
            raise_tool_error(code, "remote sync operation failed", exc)
        except Exception as exc:
            raise_tool_error(
                "INTERNAL_ERROR",
                "internal collection operation failed",
                exc,
                log_cause=True,
            )

    async def mutate(
        operation: str,
        idempotency_key: str | None,
        request: dict[str, Any],
        function: Callable[[CollectionAdapter], dict[str, Any]],
        sync_media: bool = False,
    ) -> dict[str, Any]:
        key = idempotency_key or str(uuid4())
        result = await execute(
            service.coordinated_mutation(operation, key, request, function, sync_media=sync_media)
        )
        if not isinstance(result, dict):  # pragma: no cover - coordinator always returns a receipt
            raise RuntimeError("mutation coordinator returned an invalid receipt")
        return result

    async def preview(
        operation: str,
        request: dict[str, Any],
        function: Callable[[CollectionAdapter], dict[str, Any]],
    ) -> dict[str, Any]:
        impact = await execute(service.coordinated_read(function))
        if not isinstance(impact, dict):  # pragma: no cover - previews always return mappings
            raise RuntimeError("impact preview returned an invalid result")
        return {
            "operation": operation,
            "impact": impact,
            "confirmation_token": confirmations.issue(
                operation, {"request": request, "impact": impact}
            ),
            "expires_in_seconds": settings.confirmation_ttl_seconds,
        }

    async def confirm_with_human(
        operation: str,
        details: dict[str, Any],
        *,
        always: bool = False,
        manual: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        """Ask the person to approve a call; `always` (full syncs) ignores the mode."""
        if settings.confirmation_mode == "token" and not always:
            return
        warning = f"\n\n{_FULL_SYNC_WARNING}" if always else ""
        message = f"Allow {operation}?{warning}\n\n" + json.dumps(
            details, ensure_ascii=False, indent=2
        )
        approved = await _ask_person(mcp.get_context(), message, settings.confirmation_ttl_seconds)
        if approved is None:
            if always and manual is not None:
                cause = ConfirmationRequiredError(
                    f"{operation} forces a full sync and this client cannot ask the person "
                    "to approve it; give them these steps to make the change in Anki"
                )
                raise_tool_error(
                    "MANUAL_CHANGE_REQUIRED", str(cause), cause, details=await manual()
                )
            if always or settings.confirmation_mode == "elicit_required":
                cause = ConfirmationRequiredError(
                    f"{operation} needs the person's approval and this client cannot ask"
                )
                raise_tool_error("DESTRUCTIVE_CONFIRMATION_REQUIRED", str(cause), cause)
            return
        if not approved:
            cause = ConfirmationRequiredError(f"the person did not approve {operation}")
            raise_tool_error("CONFIRMATION_DECLINED", str(cause), cause)

    async def guarded_mutate(
        operation: str,
        idempotency_key: str | None,
        request: dict[str, Any],
        confirmation_token: str,
        guard_request: dict[str, Any],
        preview_function: Callable[[CollectionAdapter], dict[str, Any]],
        function: Callable[[CollectionAdapter], dict[str, Any]],
        *,
        sync_media: bool = False,
    ) -> dict[str, Any]:
        def guarded(adapter: CollectionAdapter) -> dict[str, Any]:
            current_impact = preview_function(adapter)
            confirmation_request = {"request": guard_request, "impact": current_impact}
            try:
                confirmations.validate(confirmation_token, operation, confirmation_request)
            except ValueError as exc:
                raise ConfirmationRequiredError(f"{exc}; run preview again") from exc

            def confirmed_mutation() -> dict[str, Any]:
                try:
                    confirmations.consume(confirmation_token, operation, confirmation_request)
                except ValueError as exc:
                    raise ConfirmationRequiredError(f"{exc}; run preview again") from exc
                return function(adapter)

            return adapter.backup_before(confirmed_mutation)

        # A spent token never mutates: it replays a stored receipt or the guard refuses it.
        previewed = confirmations.describe(confirmation_token)
        if previewed is not None:
            change = guard_request.get("request", {})

            async def manual_steps() -> dict[str, Any]:
                note_type = await execute(
                    service.coordinated_read(
                        lambda adapter: adapter.get_note_type(change["note_type_id"])
                    )
                )
                return _manual_note_type_steps(operation, note_type, change["mappings"])

            manual = manual_steps if operation in conditional_sync_tool_names else None

            await confirm_with_human(
                operation,
                previewed,
                always=previewed["impact"].get("full_sync_required") is True,
                manual=manual,
            )
        return await mutate(
            operation,
            idempotency_key,
            request,
            guarded,
            sync_media=sync_media,
        )

    @scoped_tool(name="anki_status", scope="read")
    async def status() -> dict[str, Any]:
        """Return actionable local collection, authentication, sync, and recovery status."""
        return await execute(service.status())

    @scoped_tool(name="anki_operations_list", scope="read")
    async def operations_list(
        offset: Offset = 0, limit: PageLimit = settings.max_page_size
    ) -> dict[str, Any]:
        """List durable mutation operations and their synchronization state."""
        return await execute(service.list_operations(offset, limit))

    @scoped_tool(name="anki_operations_get", scope="read")
    async def operations_get(idempotency_key: IdempotencyKey) -> dict[str, Any]:
        """Get one durable mutation operation by idempotency key."""
        return await execute(service.get_operation(idempotency_key))

    @scoped_tool(name="anki_metrics", scope="read")
    async def metrics() -> dict[str, Any]:
        """Return content-free durable mutation and synchronization metrics."""
        return await execute(service.metrics())

    @scoped_tool(name="anki_sync_login", scope="admin")
    async def sync_login() -> dict[str, Any]:
        """Authenticate to the configured AnkiWeb or self-hosted sync endpoint."""
        if not settings.sync_username.strip():
            cause = ValueError("ANKI_SYNC_USERNAME is not configured")
            raise_tool_error("INVALID_ARGUMENT", str(cause), cause)
        if settings.sync_password is None:
            cause = ValueError("ANKI_SYNC_PASSWORD is not configured")
            raise_tool_error("INVALID_ARGUMENT", str(cause), cause)
        return await execute(
            service.sync_login(
                settings.sync_username,
                settings.sync_password.get_secret_value(),
                settings.sync_endpoint,
            )
        )

    @scoped_tool(name="anki_sync", scope="write")
    async def sync(sync_media: SyncMedia = True) -> dict[str, Any]:
        """Synchronize the collection with the authenticated remote server.

        Media sync runs in the background and is not waited on past a short bound. Check
        `media_sync.state` in the result: `in_progress` means it is still running (call
        `anki_status` to follow it), `completed` means it finished, `failed` carries an error code.
        """
        return await execute(service.sync(sync_media))

    @scoped_tool(
        name="anki_sync_full_download",
        scope="admin",
        enabled=settings.allow_full_sync,
        sync="full",
    )
    async def sync_full_download(confirm: Confirmation = False) -> dict[str, Any]:
        """Replace local data after the server requires a full download and confirmation is true."""
        if not confirm:
            cause = ValueError("confirm must be true for full download")
            raise_tool_error("INVALID_ARGUMENT", str(cause), cause)
        pending = (await service.status())["pending_mutations"]
        await confirm_with_human(
            "anki_sync_full_download",
            {"replaces": "local collection", "unsynced_operations_lost": pending},
            always=True,
        )
        return await execute(service.full_sync(upload=False))

    @scoped_tool(
        name="anki_sync_full_upload",
        scope="admin",
        enabled=settings.allow_full_sync,
        sync="full",
    )
    async def sync_full_upload(confirm: Confirmation = False) -> dict[str, Any]:
        """Replace remote data after the server requires a full upload and confirmation is true."""
        if not confirm:
            cause = ValueError("confirm must be true for full upload")
            raise_tool_error("INVALID_ARGUMENT", str(cause), cause)
        await confirm_with_human(
            "anki_sync_full_upload", {"replaces": "remote collection"}, always=True
        )
        return await execute(service.full_sync(upload=True))

    @scoped_tool(name="anki_backup_create", scope="admin")
    async def backup_create() -> dict[str, Any]:
        """Create an explicit local collection backup in persistent storage."""
        return await execute(service.create_backup())

    @scoped_tool(name="anki_decks_list", scope="read")
    async def decks_list(
        offset: Offset = 0,
        limit: PageLimit = settings.max_page_size,
        sync_before: SyncMedia = False,
    ) -> dict[str, Any]:
        """List decks with stable IDs and hierarchy, using bounded offset pagination."""
        return await execute(
            service.coordinated_read(
                lambda adapter: adapter.list_decks(offset=offset, limit=limit), sync_before
            )
        )

    @scoped_tool(name="anki_decks_get", scope="read")
    async def decks_get(
        deck_id: StableId | None = None,
        name: DeckName | None = None,
        sync_before: SyncMedia = False,
    ) -> dict[str, Any]:
        """Get metadata for one deck by stable Anki deck ID or by full deck name (exactly one)."""
        return await execute(
            service.coordinated_read(lambda adapter: adapter.get_deck(deck_id, name), sync_before)
        )

    @scoped_tool(name="anki_deck_options_get", scope="read")
    async def deck_options_get(
        deck_id: StableId,
        include_sections: DeckOptionSections = (),
        sync_before: SyncMedia = False,
    ) -> dict[str, Any]:
        """Get compact layered deck options, with optional counts, parents, and globals."""
        return await execute(
            service.coordinated_read(
                lambda adapter: adapter.get_deck_options(deck_id, include_sections), sync_before
            )
        )

    @scoped_tool(name="anki_deck_presets_list", scope="read")
    async def deck_presets_list(
        offset: Offset = 0,
        limit: PageLimit = settings.max_page_size,
        sync_before: SyncMedia = False,
    ) -> dict[str, Any]:
        """List compact deck preset summaries and shared-use counts."""
        return await execute(
            service.coordinated_read(
                lambda adapter: adapter.list_deck_presets(offset, limit), sync_before
            )
        )

    @scoped_tool(name="anki_deck_presets_get", scope="read")
    async def deck_presets_get(
        config_id: StableId,
        include_sections: DeckPresetSections = (),
        sync_before: SyncMedia = False,
    ) -> dict[str, Any]:
        """Get compact preset defaults, optionally projecting detailed option groups."""
        return await execute(
            service.coordinated_read(
                lambda adapter: adapter.get_deck_preset(config_id, include_sections), sync_before
            )
        )

    @scoped_tool(name="anki_decks_create", scope="write")
    async def decks_create(
        name: DeckName, idempotency_key: IdempotencyKey | None = None
    ) -> dict[str, Any]:
        """Create a deck by name, or return the existing deck with that name."""
        return await mutate(
            "anki_decks_create",
            idempotency_key,
            {"name": name},
            lambda adapter: adapter.create_deck(name),
        )

    @scoped_tool(name="anki_decks_update", scope="write")
    async def decks_update(
        deck_id: StableId,
        name: DeckName | None = None,
        description: CardText | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Rename a deck, set its description, or both. Omitted values are left unchanged."""
        return await mutate(
            "anki_decks_update",
            idempotency_key,
            {"deck_id": deck_id, "name": name, "description": description},
            lambda adapter: adapter.update_deck(deck_id, name, description),
        )

    @scoped_tool(name="anki_decks_update_config", scope="admin", destructive=True)
    async def decks_update_config(
        deck_id: StableId,
        new_cards_per_day: DailyLimit | None = None,
        reviews_per_day: DailyLimit | None = None,
        max_answer_seconds: NonNegativeInt | None = None,
        desired_retention: Retention | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Update supported bounded options on a deck's shared configuration."""
        request = {
            "deck_id": deck_id,
            "new_cards_per_day": new_cards_per_day,
            "reviews_per_day": reviews_per_day,
            "max_answer_seconds": max_answer_seconds,
            "desired_retention": desired_retention,
        }
        await confirm_with_human("anki_decks_update_config", request)
        return await mutate(
            "anki_decks_update_config",
            idempotency_key,
            request,
            lambda adapter: adapter.update_deck_config(
                deck_id,
                new_cards_per_day,
                reviews_per_day,
                max_answer_seconds,
                desired_retention,
            ),
        )

    @scoped_tool(name="anki_deck_limits_update", scope="admin", destructive=True)
    async def deck_limits_update(
        deck_id: StableId,
        update: DeckLimitUpdate,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Patch or clear validated this-deck or today-only limits for one deck."""
        value_dict = (
            update.values.model_dump(exclude_none=True) if update.values is not None else {}
        )
        request = {
            "deck_id": deck_id,
            "update": {
                "scope": update.scope,
                "values": value_dict,
                "clear_fields": list(update.clear_fields),
            },
        }
        await confirm_with_human("anki_deck_limits_update", request)
        return await mutate(
            "anki_deck_limits_update",
            idempotency_key,
            request,
            lambda adapter: adapter.update_deck_limits(
                deck_id,
                update.scope,
                value_dict,
                update.clear_fields,
            ),
        )

    @scoped_tool(name="anki_scheduler_settings_update", scope="admin", destructive=True)
    async def scheduler_settings_update(
        apply_all_parent_limits: StrictBool | None = None,
        new_cards_ignore_review_limit: StrictBool | None = None,
        fsrs_enabled: StrictBool | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Patch collection-wide scheduler and FSRS enablement settings."""
        request = {
            "apply_all_parent_limits": apply_all_parent_limits,
            "new_cards_ignore_review_limit": new_cards_ignore_review_limit,
            "fsrs_enabled": fsrs_enabled,
        }
        await confirm_with_human("anki_scheduler_settings_update", request)
        return await mutate(
            "anki_scheduler_settings_update",
            idempotency_key,
            request,
            lambda adapter: adapter.update_deck_scheduler_settings(
                apply_all_parent_limits,
                new_cards_ignore_review_limit,
                fsrs_enabled,
            ),
        )

    @scoped_tool(name="anki_fsrs_optimize_preview", scope="admin")
    async def fsrs_optimize_preview(
        config_id: DeckPresetId,
        search: SearchQuery | None = None,
        health_check: StrictBool = True,
    ) -> FsrsOptimizationPreviewResult:
        """Preview native FSRS-6 optimization for a preset ID without changing it.

        Search defaults to preset param_search, then its non-suspended cards. A requested
        failed health check is rejected; health_check=false explicitly bypasses that check.
        The preview reports candidate/current parameters, training count, and affected decks.
        """
        request = {"config_id": config_id, "search": search, "health_check": health_check}
        result = await preview(
            "anki_fsrs_optimize_apply",
            request,
            lambda adapter: adapter.preview_fsrs_optimization(config_id, search, health_check),
        )
        return FsrsOptimizationPreviewResult.model_validate(result)

    @scoped_tool(name="anki_fsrs_optimize_apply", scope="admin", destructive=True)
    async def fsrs_optimize_apply(
        config_id: DeckPresetId,
        confirmation_token: ConfirmationToken,
        idempotency_key: IdempotencyKey,
        search: SearchQuery | None = None,
        health_check: StrictBool = True,
    ) -> FsrsMutationReceipt[FsrsOptimizationApplied]:
        """Apply a matching optimization preview to every deck sharing one preset ID.

        Use identical preview arguments. idempotency_key is required, stable across retries of
        this logical request, and replays return the durable receipt.
        """
        guard_request = {"config_id": config_id, "search": search, "health_check": health_check}
        request = {**guard_request, "confirmation_token": confirmation_token}

        def guarded(adapter: CollectionAdapter) -> dict[str, Any]:
            current_impact = adapter.preview_fsrs_optimization(config_id, search, health_check)
            try:
                confirmations.consume(
                    confirmation_token,
                    "anki_fsrs_optimize_apply",
                    {"request": guard_request, "impact": current_impact},
                )
            except ValueError as exc:
                raise ConfirmationRequiredError(f"{exc}; run optimization preview again") from exc
            return adapter.apply_fsrs_optimization(
                config_id, search, health_check, impact=current_impact
            )

        previewed = confirmations.describe(confirmation_token)
        if previewed is not None:
            await confirm_with_human("anki_fsrs_optimize_apply", previewed)
        result = await mutate("anki_fsrs_optimize_apply", idempotency_key, request, guarded)
        return FsrsMutationReceipt[FsrsOptimizationApplied].model_validate(result)

    @scoped_tool(name="anki_fsrs_simulate", scope="read")
    async def fsrs_simulate(
        config_id: DeckPresetId,
        mode: FsrsSimulationMode,
        deck_size: FsrsSimulationDeckSize,
        days_to_simulate: FsrsSimulationDays,
        desired_retention: Retention | None = None,
        search: SearchQuery | None = None,
        include_daily: StrictBool = False,
        sync_before: SyncMedia = False,
    ) -> FsrsSimulationResult:
        """Simulate a preset ID with Anki 26.5 native FSRS without changing the collection.

        review estimates reviews/new cards/time seconds and knowledge acquisition; workload
        maps retention to cost seconds, memorized cards, and review count; optimal_retention
        estimates a target. Search defaults to preset param_search, then non-suspended cards.
        deck_size is hypothetical; include_daily returns exactly days_to_simulate rows.
        """
        result = await execute(
            service.coordinated_read(
                lambda adapter: adapter.simulate_fsrs(
                    config_id,
                    mode,
                    deck_size,
                    days_to_simulate,
                    desired_retention,
                    search,
                    include_daily,
                ),
                sync_before,
            )
        )
        return cast(FsrsSimulationResult, result)

    @scoped_tool(
        name="anki_fsrs_reschedule_preview",
        scope="destructive",
        enabled=settings.allow_destructive,
    )
    async def fsrs_reschedule_preview(
        config_id: DeckPresetId,
        desired_retention: Retention | None = None,
        parameters: FsrsRescheduleParameters | None = None,
    ) -> FsrsReschedulePreviewResult:
        """Preview native rescheduling after changing one shared preset ID.

        FSRS must first be enabled with anki_scheduler_settings_update. parameters is a finite
        21-value FSRS-6 vector. This destructive-scope tool requires ANKI_ALLOW_DESTRUCTIVE;
        execution requires identical change arguments and the unexpired token returned here.
        """
        request = {
            "config_id": config_id,
            "desired_retention": desired_retention,
            "parameters": parameters,
        }
        result = await preview(
            "anki_fsrs_reschedule",
            request,
            lambda adapter: adapter.preview_fsrs_reschedule(
                config_id, desired_retention, parameters
            ),
        )
        return FsrsReschedulePreviewResult.model_validate(result)

    @scoped_tool(
        name="anki_fsrs_reschedule",
        scope="destructive",
        enabled=settings.allow_destructive,
        destructive=True,
    )
    async def fsrs_reschedule(
        config_id: DeckPresetId,
        confirmation_token: ConfirmationToken,
        idempotency_key: IdempotencyKey,
        desired_retention: Retention | None = None,
        parameters: FsrsRescheduleParameters | None = None,
    ) -> FsrsMutationReceipt[FsrsRescheduleApplied]:
        """Apply a matching FSRS reschedule preview after a verified fresh backup.

        The finite 21-value parameters and retention must match preview. idempotency_key is
        required and must remain stable across retries of this logical request. A stale token
        requires a new preview. This affects every deck sharing the preset ID.
        """
        guard_request = {
            "config_id": config_id,
            "desired_retention": desired_retention,
            "parameters": parameters,
        }
        request = {**guard_request, "confirmation_token": confirmation_token}
        result = await guarded_mutate(
            "anki_fsrs_reschedule",
            idempotency_key,
            request,
            confirmation_token,
            guard_request,
            lambda adapter: adapter.preview_fsrs_reschedule(
                config_id, desired_retention, parameters
            ),
            lambda adapter: adapter.reschedule_fsrs(config_id, desired_retention, parameters),
        )
        return FsrsMutationReceipt[FsrsRescheduleApplied].model_validate(result)

    @scoped_tool(name="anki_deck_presets_create", scope="admin")
    async def deck_presets_create(
        name: ResourceName,
        clone_from_config_id: StableId | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Create a deck preset from Anki defaults or clone an existing preset."""
        return await mutate(
            "anki_deck_presets_create",
            idempotency_key,
            {"name": name, "clone_from_config_id": clone_from_config_id},
            lambda adapter: adapter.create_deck_preset(name, clone_from_config_id),
        )

    @scoped_tool(name="anki_deck_presets_update", scope="admin")
    async def deck_presets_update(
        config_id: StableId,
        name: ResourceName | None = None,
        options: DeckPresetPatch | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Patch only supplied fields on a shared preset; omitted options remain unchanged."""
        option_dict = options.model_dump(exclude_none=True) if options is not None else {}
        return await mutate(
            "anki_deck_presets_update",
            idempotency_key,
            {"config_id": config_id, "name": name, "options": option_dict},
            lambda adapter: adapter.update_deck_preset(config_id, name, option_dict),
        )

    @scoped_tool(name="anki_deck_presets_assign", scope="admin")
    async def deck_presets_assign(
        deck_id: StableId,
        config_id: StableId,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Assign an existing shared preset to one normal deck."""
        return await mutate(
            "anki_deck_presets_assign",
            idempotency_key,
            {"deck_id": deck_id, "config_id": config_id},
            lambda adapter: adapter.assign_deck_preset(deck_id, config_id),
        )

    @scoped_tool(
        name="anki_decks_delete_preview",
        scope="destructive",
        enabled=settings.allow_destructive,
    )
    async def decks_delete_preview(deck_id: StableId) -> dict[str, Any]:
        """Preview the decks, cards, and notes affected by a deck deletion."""
        request = {"deck_id": deck_id}
        return await preview(
            "anki_decks_delete", request, lambda adapter: adapter.preview_deck_delete(deck_id)
        )

    @scoped_tool(
        name="anki_decks_delete",
        scope="destructive",
        enabled=settings.allow_destructive,
        destructive=True,
    )
    async def decks_delete(
        deck_id: StableId,
        confirmation_token: ConfirmationToken,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Delete a deck after a matching impact preview and required backup."""
        request = {"deck_id": deck_id}
        return await guarded_mutate(
            "anki_decks_delete",
            idempotency_key,
            request,
            confirmation_token,
            request,
            lambda adapter: adapter.preview_deck_delete(deck_id),
            lambda adapter: adapter.delete_deck(deck_id),
        )

    @scoped_tool(name="anki_notes_search", scope="read")
    async def notes_search(
        query: SearchQuery = "",
        offset: Offset = 0,
        limit: PageLimit = settings.max_page_size,
        include_fields: StrictBool = False,
        sync_before: SyncMedia = False,
    ) -> dict[str, Any]:
        """Search notes with Anki search syntax and bounded offset pagination.

        With include_fields, each item is the full note as anki_notes_get returns it. A page
        that would exceed MCP_MAX_RESPONSE_BYTES returns fewer items, with limit set to the
        count returned and has_more true.
        """
        return await execute(
            service.coordinated_read(
                lambda adapter: fit_page(
                    adapter.search_notes(query, offset, limit, include_fields)
                ),
                sync_before,
            )
        )

    @scoped_tool(name="anki_notes_get", scope="read")
    async def notes_get(note_id: StableId, sync_before: SyncMedia = False) -> dict[str, Any]:
        """Get general note fields, tags, note type, cards, and metadata by stable ID."""
        return await execute(
            service.coordinated_read(lambda adapter: adapter.get_note(note_id), sync_before)
        )

    @scoped_tool(name="anki_notes_create", scope="write")
    async def notes_create(
        deck_id: StableId,
        note_type_id: StableId,
        fields: NoteFields,
        tags: Tags | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Create one validated, duplicate-checked note with a durable idempotent receipt."""
        normalized_tags = tags or []
        request = {
            "deck_id": deck_id,
            "note_type_id": note_type_id,
            "fields": fields,
            "tags": normalized_tags,
        }
        return await mutate(
            "anki_notes_create",
            idempotency_key,
            request,
            lambda adapter: adapter.create_note(deck_id, note_type_id, fields, normalized_tags),
        )

    @scoped_tool(name="anki_notes_create_batch", scope="write")
    async def notes_create_batch(
        notes: list[NoteCreateInput],
        skip_duplicates: bool = False,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Create a bounded atomic batch of validated notes with one idempotency key.

        With skip_duplicates, notes whose first field duplicates an existing note (or an
        earlier note in the batch) are skipped instead of failing the batch; each result in
        request order has status "created" (with note_id and card_ids) or "duplicate" (with
        duplicate_of, the id of the note it duplicates).
        Any other error still fails the whole batch.
        """
        if not notes or len(notes) > settings.max_batch_size:
            cause = ValueError(f"notes must contain between 1 and {settings.max_batch_size} items")
            raise_tool_error("INVALID_ARGUMENT", str(cause), cause)
        requests = [note.model_dump() for note in notes]
        return await mutate(
            "anki_notes_create_batch",
            idempotency_key,
            {"notes": requests, "skip_duplicates": skip_duplicates},
            lambda adapter: adapter.create_notes_batch(requests, skip_duplicates=skip_duplicates),
        )

    @scoped_tool(name="anki_notes_update_fields", scope="write")
    async def notes_update_fields(
        note_id: StableId,
        fields: NoteFields,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Patch named fields on a general note after note-type validation."""
        return await mutate(
            "anki_notes_update_fields",
            idempotency_key,
            {"note_id": note_id, "fields": fields},
            lambda adapter: adapter.update_note_fields(note_id, fields),
        )

    @scoped_tool(name="anki_notes_add_tags", scope="write")
    async def notes_add_tags(
        note_ids: StableIds,
        tags: Tags,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Add normalized tags to a bounded set of notes."""
        return await mutate(
            "anki_notes_add_tags",
            idempotency_key,
            {"note_ids": note_ids, "tags": tags},
            lambda adapter: adapter.add_note_tags(note_ids, tags),
        )

    @scoped_tool(name="anki_notes_remove_tags", scope="write")
    async def notes_remove_tags(
        note_ids: StableIds,
        tags: Tags,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Remove normalized tags from a bounded set of notes."""
        return await mutate(
            "anki_notes_remove_tags",
            idempotency_key,
            {"note_ids": note_ids, "tags": tags},
            lambda adapter: adapter.remove_note_tags(note_ids, tags),
        )

    @scoped_tool(
        name="anki_notes_delete_preview",
        scope="destructive",
        enabled=settings.allow_destructive,
    )
    async def notes_delete_preview(note_ids: StableIds) -> dict[str, Any]:
        """Preview the notes and generated cards affected by note deletion."""
        request = {"note_ids": note_ids}
        return await preview(
            "anki_notes_delete", request, lambda adapter: adapter.preview_notes_delete(note_ids)
        )

    @scoped_tool(
        name="anki_notes_delete",
        scope="destructive",
        enabled=settings.allow_destructive,
        destructive=True,
    )
    async def notes_delete(
        note_ids: StableIds,
        confirmation_token: ConfirmationToken,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Delete notes after a matching impact preview and required backup."""
        request = {"note_ids": note_ids}
        return await guarded_mutate(
            "anki_notes_delete",
            idempotency_key,
            request,
            confirmation_token,
            request,
            lambda adapter: adapter.preview_notes_delete(note_ids),
            lambda adapter: adapter.delete_notes(note_ids),
        )

    @scoped_tool(name="anki_tags_list", scope="read")
    async def tags_list(
        offset: Offset = 0,
        limit: PageLimit = settings.max_page_size,
        sync_before: SyncMedia = False,
    ) -> dict[str, Any]:
        """List collection tags with bounded offset pagination."""
        return await execute(
            service.coordinated_read(lambda adapter: adapter.list_tags(offset, limit), sync_before)
        )

    @scoped_tool(name="anki_tags_rename", scope="write")
    async def tags_rename(
        old_name: Tag,
        new_name: Tag,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Rename a tag across the collection."""
        return await mutate(
            "anki_tags_rename",
            idempotency_key,
            {"old_name": old_name, "new_name": new_name},
            lambda adapter: adapter.rename_tag(old_name, new_name),
        )

    @scoped_tool(
        name="anki_tags_delete_preview",
        scope="destructive",
        enabled=settings.allow_destructive,
    )
    async def tags_delete_preview(name: Tag) -> dict[str, Any]:
        """Preview how many notes will lose a collection tag."""
        request = {"name": name}
        return await preview(
            "anki_tags_delete", request, lambda adapter: adapter.preview_tag_delete(name)
        )

    @scoped_tool(
        name="anki_tags_delete",
        scope="destructive",
        enabled=settings.allow_destructive,
        destructive=True,
    )
    async def tags_delete(
        name: Tag,
        confirmation_token: ConfirmationToken,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Remove a tag after a matching impact preview and required backup."""
        request = {"name": name}
        return await guarded_mutate(
            "anki_tags_delete",
            idempotency_key,
            request,
            confirmation_token,
            request,
            lambda adapter: adapter.preview_tag_delete(name),
            lambda adapter: adapter.delete_tag(name),
        )

    @scoped_tool(name="anki_cards_search", scope="read")
    async def cards_search(
        query: SearchQuery = "",
        offset: Offset = 0,
        limit: PageLimit = settings.max_page_size,
        sync_before: SyncMedia = False,
    ) -> dict[str, Any]:
        """Search cards with Anki search syntax and bounded offset pagination."""
        return await execute(
            service.coordinated_read(
                lambda adapter: adapter.search_cards(query=query, offset=offset, limit=limit),
                sync_before,
            )
        )

    @scoped_tool(name="anki_cards_get", scope="read")
    async def cards_get(
        card_id: StableId,
        include_sections: CardGetSections = (),
        sync_before: SyncMedia = False,
    ) -> dict[str, Any]:
        """Get card content and scheduling, with optional review_summary and fsrs sections."""
        return await execute(
            service.coordinated_read(
                lambda adapter: adapter.get_card(card_id, include_sections), sync_before
            )
        )

    @scoped_tool(name="anki_reviews_list", scope="read")
    async def reviews_list(
        card_id: StableId | None = None,
        deck_id: StableId | None = None,
        query: SearchQuery | None = None,
        include_children: StrictBool = False,
        offset: Offset = 0,
        limit: PageLimit = settings.max_page_size,
        order: ReviewOrder = "newest",
        include_fields: ReviewEventFields = (),
        sync_before: SyncMedia = False,
    ) -> dict[str, Any]:
        """List compact review events, optionally including requested detail fields."""
        return await execute(
            service.coordinated_read(
                lambda adapter: adapter.list_reviews(
                    card_id,
                    deck_id,
                    query,
                    include_children,
                    offset,
                    limit,
                    order,
                    include_fields,
                ),
                sync_before,
            )
        )

    @scoped_tool(name="anki_review_stats", scope="read")
    async def review_stats(
        card_id: StableId | None = None,
        deck_id: StableId | None = None,
        query: SearchQuery | None = None,
        include_children: StrictBool = False,
        days: ReviewDays = 30,
        include_sections: ReviewStatsSections = (),
        sync_before: SyncMedia = False,
    ) -> dict[str, Any]:
        """Return compact review analytics with optional detailed graph sections."""
        return await execute(
            service.coordinated_read(
                lambda adapter: adapter.review_stats(
                    card_id, deck_id, query, include_children, days, include_sections
                ),
                sync_before,
            )
        )

    @scoped_tool(name="anki_cards_create", scope="write")
    async def cards_create(
        deck_id: StableId,
        front: CardText,
        back: CardText,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Create one Basic note/card in a deck."""
        return await mutate(
            "anki_cards_create",
            idempotency_key,
            {"deck_id": deck_id, "front": front, "back": back},
            lambda adapter: adapter.create_card(deck_id, front, back),
        )

    @scoped_tool(name="anki_cards_update", scope="write")
    async def cards_update(
        card_id: StableId,
        front: CardText | None = None,
        back: CardText | None = None,
        deck_id: StableId | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Update a Basic card's Front/Back fields and/or move it to another deck."""
        return await mutate(
            "anki_cards_update",
            idempotency_key,
            {"card_id": card_id, "front": front, "back": back, "deck_id": deck_id},
            lambda adapter: adapter.update_card(card_id, front, back, deck_id),
        )

    @scoped_tool(name="anki_cards_change_deck", scope="write")
    async def cards_change_deck(
        card_ids: StableIds,
        deck_id: StableId,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Move arbitrary supported cards to another deck by stable IDs."""
        return await mutate(
            "anki_cards_change_deck",
            idempotency_key,
            {"card_ids": card_ids, "deck_id": deck_id},
            lambda adapter: adapter.change_card_deck(card_ids, deck_id),
        )

    @scoped_tool(name="anki_cards_suspend", scope="write")
    async def cards_suspend(
        card_ids: StableIds, idempotency_key: IdempotencyKey | None = None
    ) -> dict[str, Any]:
        """Suspend arbitrary supported cards by stable IDs."""
        return await mutate(
            "anki_cards_suspend",
            idempotency_key,
            {"card_ids": card_ids},
            lambda adapter: adapter.suspend_cards(card_ids),
        )

    @scoped_tool(name="anki_cards_unsuspend", scope="write")
    async def cards_unsuspend(
        card_ids: StableIds, idempotency_key: IdempotencyKey | None = None
    ) -> dict[str, Any]:
        """Unsuspend arbitrary supported cards by stable IDs."""
        return await mutate(
            "anki_cards_unsuspend",
            idempotency_key,
            {"card_ids": card_ids},
            lambda adapter: adapter.unsuspend_cards(card_ids),
        )

    @scoped_tool(name="anki_cards_set_flag", scope="write")
    async def cards_set_flag(
        card_ids: StableIds,
        flag: CardFlag,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Set or clear the flag on a bounded set of cards."""
        return await mutate(
            "anki_cards_set_flag",
            idempotency_key,
            {"card_ids": card_ids, "flag": flag},
            lambda adapter: adapter.set_card_flag(card_ids, flag),
        )

    @scoped_tool(name="anki_cards_reposition", scope="write")
    async def cards_reposition(
        card_ids: StableIds,
        starting_from: PositiveInt,
        step_size: PositiveInt = 1,
        randomize: StrictBool = False,
        shift_existing: StrictBool = True,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Reposition a bounded set of new cards using explicit scheduling controls."""
        request = {
            "card_ids": card_ids,
            "starting_from": starting_from,
            "step_size": step_size,
            "randomize": randomize,
            "shift_existing": shift_existing,
        }
        return await mutate(
            "anki_cards_reposition",
            idempotency_key,
            request,
            lambda adapter: adapter.reposition_cards(
                card_ids, starting_from, step_size, randomize, shift_existing
            ),
        )

    @scoped_tool(
        name="anki_cards_answer",
        scope="admin",
        enabled=settings.allow_review_answers,
    )
    async def cards_answer(
        card_id: StableId,
        rating: ReviewRating,
        answer_seconds: AnswerSeconds = 0,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Record a real review answer when explicitly enabled by the operator."""
        return await mutate(
            "anki_cards_answer",
            idempotency_key,
            {"card_id": card_id, "rating": rating, "answer_seconds": answer_seconds},
            lambda adapter: adapter.answer_card(card_id, rating, answer_seconds),
        )

    @scoped_tool(
        name="anki_cards_delete_preview",
        scope="destructive",
        enabled=settings.allow_destructive,
    )
    async def cards_delete_preview(card_id: StableId) -> dict[str, Any]:
        """Preview card deletion and whether its orphaned note will also be deleted."""
        request = {"card_id": card_id}
        return await preview(
            "anki_cards_delete", request, lambda adapter: adapter.preview_card_delete(card_id)
        )

    @scoped_tool(
        name="anki_cards_delete",
        scope="destructive",
        enabled=settings.allow_destructive,
        destructive=True,
    )
    async def cards_delete(
        card_id: StableId,
        confirmation_token: ConfirmationToken,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Delete one card after a matching impact preview and required backup."""
        request = {"card_id": card_id}
        return await guarded_mutate(
            "anki_cards_delete",
            idempotency_key,
            request,
            confirmation_token,
            request,
            lambda adapter: adapter.preview_card_delete(card_id),
            lambda adapter: adapter.delete_card(card_id),
        )

    @scoped_tool(name="anki_note_types_list", scope="read")
    async def note_types_list(
        offset: Offset = 0,
        limit: PageLimit = settings.max_page_size,
        sync_before: SyncMedia = False,
    ) -> dict[str, Any]:
        """List note types with stable IDs and bounded pagination."""
        return await execute(
            service.coordinated_read(
                lambda adapter: adapter.list_note_types(offset, limit), sync_before
            )
        )

    @scoped_tool(name="anki_note_types_get", scope="read")
    async def note_types_get(
        note_type_id: StableId, sync_before: SyncMedia = False
    ) -> dict[str, Any]:
        """Get fields, templates, CSS, kind, and usage for one note type."""
        return await execute(
            service.coordinated_read(
                lambda adapter: adapter.get_note_type(note_type_id), sync_before
            )
        )

    @scoped_tool(
        name="anki_note_types_change_preview",
        scope="admin",
        enabled=settings.allow_schema_changes,
    )
    async def note_types_change_preview(
        operation: SchemaChangeOperation,
        note_type_id: StableId | None = None,
        name: ResourceName | None = None,
        fields: list[ResourceName] | None = None,
        templates: list[NoteTypeTemplateInput] | None = None,
        css: CardText | None = None,
        field_mappings: FieldMappings | None = None,
        template_mappings: TemplateMappings | None = None,
        kind: NoteTypeKind | None = None,
    ) -> dict[str, Any]:
        """Preview an exact proposed schema mutation and issue a maintenance token."""
        operation_names = {
            "create": "anki_note_types_create",
            "update": "anki_note_types_update",
            "fields_update": "anki_note_type_fields_update",
            "templates_update": "anki_templates_update",
        }
        template_values = (
            [template.model_dump() for template in templates] if templates is not None else None
        )
        field_mapping_values = (
            [mapping.model_dump() for mapping in field_mappings]
            if field_mappings is not None
            else None
        )
        template_mapping_values = (
            [mapping.model_dump() for mapping in template_mappings]
            if template_mappings is not None
            else None
        )
        if operation == "create":
            if (
                note_type_id is not None
                or name is None
                or fields is None
                or template_values is None
                or field_mapping_values is not None
                or template_mapping_values is not None
            ):
                cause = ValueError(
                    "create preview requires only name, fields, templates, css, and kind"
                )
                raise_tool_error("INVALID_ARGUMENT", str(cause), cause)
            mutation_request = {
                "name": name,
                "fields": fields,
                "templates": template_values,
                "css": css or "",
                "kind": kind or "standard",
            }
        elif kind is not None:
            cause = ValueError("kind applies only to the create operation")
            raise_tool_error("INVALID_ARGUMENT", str(cause), cause)
        elif operation == "update":
            if (
                note_type_id is None
                or field_mapping_values is not None
                or template_mapping_values is not None
            ):
                cause = ValueError(
                    "update preview requires note_type_id and any of name, fields, templates, css"
                )
                raise_tool_error("INVALID_ARGUMENT", str(cause), cause)
            mutation_request = note_type_update_request(
                note_type_id, name, fields, template_values, css
            )
        elif operation == "fields_update":
            if (
                note_type_id is None
                or field_mapping_values is None
                or name is not None
                or fields is not None
                or template_values is not None
                or template_mapping_values is not None
                or css
            ):
                cause = ValueError(
                    "fields_update preview requires only note_type_id and field_mappings"
                )
                raise_tool_error("INVALID_ARGUMENT", str(cause), cause)
            mutation_request = {
                "note_type_id": note_type_id,
                "mappings": field_mapping_values,
            }
        else:
            if (
                note_type_id is None
                or template_mapping_values is None
                or name is not None
                or fields is not None
                or template_values is not None
                or field_mapping_values is not None
                or css
            ):
                cause = ValueError(
                    "templates_update preview requires only note_type_id and template_mappings"
                )
                raise_tool_error("INVALID_ARGUMENT", str(cause), cause)
            mutation_request = {
                "note_type_id": note_type_id,
                "mappings": template_mapping_values,
            }
        guard_request = {"operation": operation, "request": mutation_request}
        return await preview(
            operation_names[operation],
            guard_request,
            lambda adapter: adapter.preview_note_type_change(
                operation,
                note_type_id,
                [m["source_ordinal"] for m in mutation_request.get("mappings", [])] or None,
            ),
        )

    @scoped_tool(
        name="anki_note_types_create",
        scope="admin",
        enabled=settings.allow_schema_changes,
    )
    async def note_types_create(
        name: ResourceName,
        fields: list[ResourceName],
        templates: list[NoteTypeTemplateInput],
        confirmation_token: ConfirmationToken,
        css: CardText = "",
        kind: NoteTypeKind = "standard",
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Create a standard or cloze note type after preview, backup, and full-sync gating."""
        template_values = [template.model_dump() for template in templates]
        request = {
            "name": name,
            "fields": fields,
            "templates": template_values,
            "css": css,
            "kind": kind,
        }
        return await guarded_mutate(
            "anki_note_types_create",
            idempotency_key,
            request,
            confirmation_token,
            {"operation": "create", "request": request},
            lambda adapter: adapter.preview_note_type_change("create", None),
            lambda adapter: adapter.create_note_type(name, fields, template_values, css, kind),
        )

    @scoped_tool(
        name="anki_note_types_update",
        scope="admin",
        enabled=settings.allow_schema_changes,
        destructive=True,
    )
    async def note_types_update(
        note_type_id: StableId,
        confirmation_token: ConfirmationToken,
        name: ResourceName | None = None,
        fields: list[ResourceName] | None = None,
        templates: list[NoteTypeTemplateInput] | None = None,
        css: CardText | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Change a note type's name, fields, templates, or CSS after guarded approval.

        Omitted values are left unchanged.
        """
        template_values = (
            [template.model_dump() for template in templates] if templates is not None else None
        )
        request = note_type_update_request(note_type_id, name, fields, template_values, css)
        return await guarded_mutate(
            "anki_note_types_update",
            idempotency_key,
            request,
            confirmation_token,
            {"operation": "update", "request": request},
            lambda adapter: adapter.preview_note_type_change("update", note_type_id),
            lambda adapter: adapter.update_note_type(
                note_type_id, name, fields, template_values, css
            ),
        )

    @scoped_tool(
        name="anki_note_type_fields_update",
        scope="admin",
        enabled=settings.allow_schema_changes and settings.allow_full_sync,
        destructive=True,
        sync="conditional",
    )
    async def note_type_fields_update(
        note_type_id: StableId,
        mappings: FieldMappings,
        confirmation_token: ConfirmationToken,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Change fields after guarded full-sync maintenance approval."""
        mapping_values = [mapping.model_dump() for mapping in mappings]
        request = {"note_type_id": note_type_id, "mappings": mapping_values}
        return await guarded_mutate(
            "anki_note_type_fields_update",
            idempotency_key,
            request,
            confirmation_token,
            {"operation": "fields_update", "request": request},
            lambda adapter: adapter.preview_note_type_change(
                "fields_update", note_type_id, [m["source_ordinal"] for m in mapping_values]
            ),
            lambda adapter: adapter.update_note_type_fields(note_type_id, mapping_values),
        )

    @scoped_tool(
        name="anki_templates_update",
        scope="admin",
        enabled=settings.allow_schema_changes and settings.allow_full_sync,
        destructive=True,
        sync="conditional",
    )
    async def templates_update(
        note_type_id: StableId,
        mappings: TemplateMappings,
        confirmation_token: ConfirmationToken,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Change templates after guarded full-sync maintenance approval."""
        mapping_values = [mapping.model_dump() for mapping in mappings]
        request = {"note_type_id": note_type_id, "mappings": mapping_values}
        return await guarded_mutate(
            "anki_templates_update",
            idempotency_key,
            request,
            confirmation_token,
            {"operation": "templates_update", "request": request},
            lambda adapter: adapter.preview_note_type_change(
                "templates_update", note_type_id, [m["source_ordinal"] for m in mapping_values]
            ),
            lambda adapter: adapter.update_templates(note_type_id, mapping_values),
        )

    @scoped_tool(name="anki_media_list", scope="read")
    async def media_list(
        offset: Offset = 0,
        limit: PageLimit = settings.max_page_size,
        sync_before: SyncMedia = False,
    ) -> dict[str, Any]:
        """List collection media filenames and sizes with bounded pagination."""
        return await execute(
            service.coordinated_read(
                lambda adapter: adapter.list_media(offset, limit),
                sync_before,
                sync_media=True,
            )
        )

    @scoped_tool(name="anki_media_get", scope="read")
    async def media_get(filename: MediaFilename, sync_before: SyncMedia = False) -> dict[str, Any]:
        """Read one bounded media file as base64 by safe filename."""
        return await execute(
            service.coordinated_read(
                lambda adapter: adapter.get_media(filename), sync_before, sync_media=True
            )
        )

    @scoped_tool(name="anki_media_check", scope="read")
    async def media_check(
        offset: Offset = 0,
        limit: PageLimit = settings.max_page_size,
        sync_before: SyncMedia = False,
    ) -> dict[str, Any]:
        """Report bounded missing and unused media after optional synchronization."""
        return await execute(
            service.coordinated_read(
                lambda adapter: adapter.check_media(offset, limit),
                sync_before,
                sync_media=True,
            )
        )

    @scoped_tool(name="anki_media_store", scope="write")
    async def media_store(
        filename: MediaFilename,
        content_base64: MediaContent,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Create or replace one bounded media file from base64 content."""
        return await mutate(
            "anki_media_store",
            idempotency_key,
            {"filename": filename, "content_base64": content_base64},
            lambda adapter: adapter.store_media(filename, content_base64),
            sync_media=True,
        )

    @scoped_tool(name="anki_media_rename", scope="write")
    async def media_rename(
        old_filename: MediaFilename,
        new_filename: MediaFilename,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Rename one bounded media file without overwriting an existing target."""
        return await mutate(
            "anki_media_rename",
            idempotency_key,
            {"old_filename": old_filename, "new_filename": new_filename},
            lambda adapter: adapter.rename_media(old_filename, new_filename),
            sync_media=True,
        )

    @scoped_tool(
        name="anki_media_delete_preview",
        scope="destructive",
        enabled=settings.allow_destructive,
    )
    async def media_delete_preview(filename: MediaFilename) -> dict[str, Any]:
        """Preview the bounded media file that will be moved to Anki's trash."""
        request = {"filename": filename}
        return await preview(
            "anki_media_delete", request, lambda adapter: adapter.preview_media_delete(filename)
        )

    @scoped_tool(
        name="anki_media_delete",
        scope="destructive",
        enabled=settings.allow_destructive,
        destructive=True,
    )
    async def media_delete(
        filename: MediaFilename,
        confirmation_token: ConfirmationToken,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Move media to trash after a matching impact preview and required backup."""
        request = {"filename": filename}
        return await guarded_mutate(
            "anki_media_delete",
            idempotency_key,
            request,
            confirmation_token,
            request,
            lambda adapter: adapter.preview_media_delete(filename),
            lambda adapter: adapter.delete_media(filename),
            sync_media=True,
        )

    # FastMCP currently generates argument models with Pydantic's extra="ignore".
    # Tighten both runtime validation and the JSON schemas advertised to clients.
    for tool_name in registered_tool_names:
        registered = mcp._tool_manager.get_tool(tool_name)  # pyright: ignore[reportPrivateUsage]
        if registered is None:  # pragma: no cover
            raise RuntimeError(f"failed to register {tool_name}")
        registered.fn_metadata.arg_model.model_config["extra"] = "forbid"
        registered.fn_metadata.arg_model.model_config["hide_input_in_errors"] = True
        registered.fn_metadata.arg_model.model_rebuild(force=True)
        registered.parameters = registered.fn_metadata.arg_model.model_json_schema()
        properties = registered.parameters.get("properties", {})
        offset_schema = properties.get("offset")
        if offset_schema is not None:
            offset_schema["minimum"] = 0
        limit_schema = properties.get("limit")
        if limit_schema is not None:
            limit_schema["minimum"] = 1
            limit_schema["maximum"] = settings.max_page_size
        for id_name in ("deck_id", "card_id", "note_id", "note_type_id"):
            id_schema = properties.get(id_name)
            if id_schema is not None:
                id_schema["minimum"] = 1

    original_call_tool = mcp._tool_manager.call_tool  # pyright: ignore[reportPrivateUsage]

    async def call_tool_with_stable_validation_errors(
        name: str,
        arguments: dict[str, Any],
        context: Any = None,
        convert_result: bool = False,
    ) -> Any:
        try:
            return await original_call_tool(
                name, arguments, context=context, convert_result=convert_result
            )
        except ToolError as exc:
            if isinstance(exc.__cause__, ValidationError):
                raise_tool_error(
                    "INVALID_ARGUMENT", "tool arguments failed validation", exc.__cause__
                )
            raise

    mcp._tool_manager.call_tool = (  # pyright: ignore[reportPrivateUsage]
        call_tool_with_stable_validation_errors
    )

    mcp_app = mcp.streamable_http_app()

    async def live(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def ready(_: Request) -> Response:
        try:
            current_status = await service.status()
        except Exception:
            current_status = {"ready": False, "readiness_reason": "collection_unavailable"}
        if current_status["readiness_reason"] not in _PROCESS_READY_REASONS:
            return JSONResponse(
                {
                    "status": "not_ready",
                    "reason": current_status["readiness_reason"],
                },
                status_code=503,
            )
        return JSONResponse({"status": "ready"})

    async def drive_initial_media_sync() -> None:
        while True:
            try:
                if not await service.advance_initial_media_sync():
                    return
            except Exception:
                LOGGER.exception("initial media sync step failed")
            await asyncio.sleep(_INITIAL_MEDIA_SYNC_POLL_SECONDS)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncGenerator[None]:
        async with service, mcp_app.router.lifespan_context(mcp_app):
            if settings.bootstrap_mode != "disabled":
                password = (
                    settings.sync_password.get_secret_value()
                    if settings.sync_password is not None
                    else None
                )
                await service.bootstrap(
                    settings.bootstrap_mode,
                    settings.sync_username,
                    password,
                    settings.sync_endpoint,
                )
            driver = (
                asyncio.create_task(drive_initial_media_sync())
                if settings.initial_media_sync
                else None
            )
            try:
                yield
            finally:
                if driver is not None:
                    driver.cancel()

    routes = [
        Route("/health/live", live, methods=["GET"]),
        Route("/health/ready", ready, methods=["GET"]),
        *mcp_app.routes,
        *(oauth_provider.routes() if oauth_provider is not None else []),
    ]
    app = Starlette(routes=routes, middleware=mcp_app.user_middleware, lifespan=lifespan)
    app.state.settings = settings
    app.state.collection_service = service
    app.state.mcp = mcp
    limited: ASGIApp = RequestBodyLimitMiddleware(
        app, settings.max_request_bytes, settings.mcp_path
    )
    if oauth_provider is not None:
        for path in ("/oauth", "/register", "/token", "/revoke"):
            limited = RequestBodyLimitMiddleware(limited, _OAUTH_MAX_REQUEST_BYTES, path)
        return limited
    return BearerAuthMiddleware(
        limited,
        settings.auth_token.get_secret_value(),
        settings.mcp_path,
    )
