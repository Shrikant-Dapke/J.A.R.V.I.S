"""Persistent conversation history store (Phase 5.1-b).

Replaces the Phase 2 process-local in-memory store. Conversations and
their turns are persisted through the shared execution-state SQLite
façade (``app.services.execution_db``), so history survives server
restarts. The public contract of the previous implementation is
preserved exactly:

- ``create_conversation`` / ``add_turn`` / ``get_history`` /
  ``clear_conversation`` / ``exists`` keep their signatures, error
  types, and identity-scoped semantics; ``conversation_store`` remains
  the module singleton consumed by the chat service, orchestrator, and
  conversation API routes. ``get_history`` grew an optional ``limit``
  (most recent window, default keeps the full-history behavior), and
  metadata reads (``get_turn_count``, ``list_conversations``,
  ``count_conversations``) expose listing/pagination for the API.
- Owner/session isolation is unchanged: a conversation of another
  identity is indistinguishable from an unknown one (404 semantics),
  ownership is never rebound, and content is never readable
  cross-identity.
- New bounded-store behavior: at most ``MAX_CONVERSATIONS_PER_IDENTITY``
  conversations per identity. Exhaustion raises
  ``ConversationCapacityError`` (fail closed, never degrade into
  dropping old conversations).
- Every mutation is ONE ``BEGIN IMMEDIATE`` transaction on the shared
  façade: the capacity check and conversation creation are atomic, and
  turn append + FIFO retention + metadata updates commit or roll back
  together.
- Reads validate every stored turn and every conversation-metadata
  timestamp strictly. A damaged record raises
  ``ConversationCorruptError`` instead of silently passing, being
  fabricated, or being discarded; nothing is ever invented for a
  conversation read of corrupted storage. Deletion remains possible for
  damaged conversations: ``clear_conversation`` never has to interpret
  unreadable state to remove it. Error messages and logs carry
  identifiers only — never conversation content or credentials.
- Listing reads pair the page rows with the identity's total inside ONE
  façade transaction (``list_conversations_page``), so a response's
  page and ``total`` always come from the same consistent instant.
"""

import uuid
from datetime import datetime
from typing import Optional

from app.schemas.conversation import ConversationTurn
from app.core.logging import get_logger
from app.services.execution_db import (
    MAX_TURN_CONTENT_LENGTH,
    ConversationMessageRecord,
    ConversationRecord,
    ExecutionStateDb,
    state_db as shared_state_db,
)


logger = get_logger(__name__)


MAX_CONVERSATIONS_PER_IDENTITY = 256


class ConversationNotFoundError(LookupError):
    """Raised when a conversation is unknown or owned by another identity."""


class ConversationCapacityError(RuntimeError):
    """Raised when the bounded conversation store is exhausted."""


class ConversationCorruptError(RuntimeError):
    """Raised when persisted conversation state cannot be validated safely."""


class PersistentConversationStore:
    """Owner/session-scoped persistent store for conversation turns.

    Backed by the shared ``ExecutionStateDb`` façade. A constructor
    without an explicit façade opens an isolated in-memory database, so
    each test object graph stays independent by construction; production
    wiring uses the shared file-backed façade (see the module singleton).
    Threading safety comes from the façade: every store operation runs
    inside one façade transaction, and no store method ever calls back
    into a service that would re-enter a different lock.
    """

    def __init__(
        self,
        state_db: Optional[ExecutionStateDb] = None,
        *,
        max_conversations: int = MAX_CONVERSATIONS_PER_IDENTITY,
    ) -> None:
        if max_conversations <= 0:
            raise ValueError("max_conversations must be positive")
        self._execution_db = (
            state_db if state_db is not None else ExecutionStateDb()
        )
        self._max_conversations = max_conversations

    @property
    def execution_db(self) -> ExecutionStateDb:
        """The façade backing this store (shared with execution state)."""
        return self._execution_db

    # -- write path ------------------------------------------------------

    def _enforce_capacity(
        self, owner_id: str, session_id: str
    ) -> None:
        """Fail closed when the identity's conversation capacity is full."""
        count = self._execution_db.count_conversations(owner_id, session_id)
        if count >= self._max_conversations:
            raise ConversationCapacityError(
                "The conversation store has reached its configured "
                "capacity."
            )

    def create_conversation(
        self,
        conversation_id: Optional[str] = None,
        *,
        owner_id: str,
        session_id: str,
    ) -> str:
        """
        Create a conversation (no-op if it already exists for this
        identity).

        An existing conversation keeps its original owner, session, and
        creation timestamp. A conversation owned by ANOTHER identity is
        rejected instead of being reused: the caller observes 404
        semantics here rather than silently attaching to foreign state.
        That rejection runs BEFORE metadata validation, so a damaged
        foreign conversation stays indistinguishable from an unknown
        one.

        Returns:
            The conversation identifier (generated when not supplied).

        Raises:
            ConversationNotFoundError: If the conversation exists but is
                bound to a different owner or session.
            ConversationCapacityError: If creating would exceed the
                identity's capacity.
            ConversationCorruptError: If the caller's own conversation's
                metadata cannot be validated.
        """
        resolved = conversation_id.strip() if conversation_id else None
        if not resolved:
            resolved = str(uuid.uuid4())
        with self._execution_db.transaction():
            record = self._execution_db.get_conversation(resolved)
            if record is not None:
                if (
                    record.owner_id != owner_id
                    or record.session_id != session_id
                ):
                    raise ConversationNotFoundError(
                        "Conversation was not found"
                    )
                record = self._validated_conversation(record)
                return resolved
            self._enforce_capacity(owner_id, session_id)
            self._execution_db.upsert_conversation(resolved, owner_id, session_id)
        return resolved

    def add_turn(
        self,
        conversation_id: str,
        role: str,
        content: str,
        *,
        owner_id: str,
        session_id: str,
    ) -> ConversationTurn:
        """
        Append a turn, creating the conversation if missing.

        A conversation created in another owner/session is treated as
        not found and is never modified (including when its metadata is
        damaged: the ownership rejection runs before validation, so
        foreign damaged state is indistinguishable from unknown). The
        turn role is validated by the turn schema before any state is
        touched; the stored content is bounded and CHECK-enforced by the
        storage façade.

        Returns:
            The persisted turn, exactly as stored (fresh object).

        Raises:
            ConversationNotFoundError: If the conversation exists but is
                bound to a different owner or session.
            ConversationCapacityError: If the missing conversation would
                exceed the identity's capacity.
            ConversationCorruptError: If the caller's own conversation's
                metadata cannot be validated.
        """
        turn = ConversationTurn(role=role, content=content)
        with self._execution_db.transaction():
            record = self._execution_db.get_conversation(conversation_id)
            if record is not None and (
                record.owner_id != owner_id
                or record.session_id != session_id
            ):
                raise ConversationNotFoundError(
                    "Conversation was not found"
                )
            record = self._validated_conversation(record)
            if record is None:
                self._enforce_capacity(owner_id, session_id)
                self._execution_db.upsert_conversation(
                    conversation_id, owner_id, session_id
                )
            message = self._execution_db.append_conversation_message(
                conversation_id,
                turn.role,
                turn.content,
                created_at=turn.timestamp.isoformat(),
            )
        stored = self._turn_from_record(message)
        return stored

    # -- read path -------------------------------------------------------

    def _validated_conversation(
        self, record: Optional[ConversationRecord]
    ) -> Optional[ConversationRecord]:
        """Validate conversation metadata strictly and return it.

        ``created_at``/``updated_at`` must parse as ISO-8601.
        A damaged metadata row raises ``ConversationCorruptError``:
        corrupt state is never served, repaired, or silently dropped.
        The error carries identifiers only, never content.
        """
        if record is None:
            return None
        for field, stamp in (
            ("created_at", record.created_at),
            ("updated_at", record.updated_at),
        ):
            try:
                datetime.fromisoformat(stamp)
            except (TypeError, ValueError):
                logger.warning(
                    "Corrupted conversation metadata rejected on read",
                    extra={
                        "extra_fields": {
                            "record": "conversation",
                            "field": field,
                            "conversation_id": record.conversation_id,
                        }
                    },
                )
                raise ConversationCorruptError(
                    "Conversation history could not be validated."
                )
        return record

    def _turn_from_record(
        self, record: ConversationMessageRecord
    ) -> ConversationTurn:
        """Validate one stored row strictly and produce its turn.

        Role, content bounds, and timestamp parse are all enforced
        here. A record that fails validation raises
        ``ConversationCorruptError``: corrupted storage is never
        silently passed, repaired, or dropped, and the error carries no
        conversation content.
        """
        role = record.role
        content = record.content
        created_at = record.created_at
        corrupt = False
        if role not in ("user", "assistant"):
            corrupt = True
        if (
            not isinstance(content, str)
            or not content
            or len(content) > MAX_TURN_CONTENT_LENGTH
        ):
            corrupt = True
        timestamp: Optional[datetime] = None
        try:
            timestamp = datetime.fromisoformat(created_at)
        except (TypeError, ValueError):
            corrupt = True
        if corrupt:
            logger.warning(
                "Corrupted conversation record rejected on read",
                extra={
                    "extra_fields": {
                        "record": "conversation_message",
                        "seq": record.seq,
                    }
                },
            )
            raise ConversationCorruptError(
                "Conversation history could not be validated."
            )
        assert timestamp is not None
        return ConversationTurn(role=role, content=content, timestamp=timestamp)

    def exists(self, conversation_id: str) -> bool:
        """Check whether a conversation exists (existence, not ownership)."""
        return self._execution_db.get_conversation(conversation_id) is not None

    def get_history(
        self,
        conversation_id: str,
        *,
        owner_id: str,
        session_id: str,
        limit: Optional[int] = None,
    ) -> Optional[list[ConversationTurn]]:
        """
        Retrieve the requesting identity's chronological turns, or None if
        the conversation is unknown or owned by another owner/session.

        With ``limit`` the MOST RECENT turns form the returned window,
        still ordered oldest to newest (the full history is preserved in
        storage either way). Identity matching runs on the raw record
        BEFORE validation (foreign/unknown -> None), then the caller's
        own metadata and every turn are validated strictly on read (see
        ``_validated_conversation`` and ``_turn_from_record``) and
        rebuilt fresh, so callers can never mutate stored state through
        the returned list.
        """
        record = self._execution_db.get_conversation(conversation_id)
        if (
            record is None
            or record.owner_id != owner_id
            or record.session_id != session_id
        ):
            return None
        record = self._validated_conversation(record)
        messages = self._execution_db.list_conversation_messages(
            conversation_id, limit=limit
        )
        return [self._turn_from_record(message) for message in messages]

    def get_turn_count(
        self,
        conversation_id: str,
        *,
        owner_id: str,
        session_id: str,
    ) -> Optional[int]:
        """Persisted turn total for one identity-scoped conversation.

        Same isolation semantics as ``get_history``: None for unknown or
        foreign conversations, so callers cannot distinguish them and no
        foreign metadata is exposed. Metadata-only (never reads turns),
        but the caller's own conversation's timestamps are validated
        strictly.

        Identity matching runs on the raw record BEFORE metadata
        validation: a damaged conversation owned by another identity
        stays indistinguishable from an unknown one.
        """
        record = self._execution_db.get_conversation(conversation_id)
        if (
            record is None
            or record.owner_id != owner_id
            or record.session_id != session_id
        ):
            return None
        return self._validated_conversation(record).turn_count

    def list_conversations(
        self,
        *,
        owner_id: str,
        session_id: str,
        limit: int,
        offset: int = 0,
    ) -> list[ConversationRecord]:
        """Identity-scoped conversation metadata, newest activity first.

        Deterministic ordering from the storage façade:
        ``updated_at DESC, conversation_id DESC`` — a page is
        reproducible while rows are unchanged, which makes offset
        pagination stable. Metadata only (identity fields of rows are
        already guaranteed to be the requester's own).
        """
        records, _total = self.list_conversations_page(
            owner_id=owner_id, session_id=session_id, limit=limit, offset=offset
        )
        return records

    def list_conversations_page(
        self,
        *,
        owner_id: str,
        session_id: str,
        limit: int,
        offset: int = 0,
    ) -> tuple[list[ConversationRecord], int]:
        """Identity-scoped page + total in one consistent snapshot.

        Delegates to the façade's single-transaction page operation, so
        the returned (page, total) pair can never mix instants under
        concurrent chat traffic. Every listed conversation's metadata
        timestamps are validated strictly; damaged metadata raises
        ``ConversationCorruptError`` instead of silently serving or
        discarding the damaged row.
        """
        records, total = self._execution_db.list_conversations_page(
            owner_id, session_id, limit=limit, offset=offset
        )
        return (
            [self._validated_conversation(record) for record in records],
            total,
        )

    def count_conversations(self, owner_id: str, session_id: str) -> int:
        """Conversations persisted for exactly this identity."""
        return self._execution_db.count_conversations(owner_id, session_id)

    def clear_conversation(
        self,
        conversation_id: str,
        *,
        owner_id: str,
        session_id: str,
    ) -> bool:
        """
        Delete a conversation owned by the requesting identity.

        The identity check runs before deletion; the delete itself
        removes the conversation and every turn in one transaction and
        persists across restarts.

        Returns:
            True if deleted, False if unknown or owned by another identity.
        """
        record = self._execution_db.get_conversation(conversation_id)
        if (
            record is None
            or record.owner_id != owner_id
            or record.session_id != session_id
        ):
            return False
        return self._execution_db.delete_conversation(conversation_id)


conversation_store = PersistentConversationStore(state_db=shared_state_db)
