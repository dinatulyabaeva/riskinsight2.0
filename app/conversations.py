"""Bounded, server-owned conversational context; never a source of portfolio numbers.

One process, no persistence promise: a restart/TTL expires a dialog explicitly.
The only authoritative numbers for a turn are fresh MCP results for its dataset.
"""
import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import time
import uuid


class ConversationConflict(ValueError):
    pass


@dataclass
class Conversation:
    identifier: str
    scope_key: str
    updated: float
    turns: list = field(default_factory=list)
    last_scope: dict = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def memory(self):
        return deepcopy({"turns": self.turns, "last_scope": self.last_scope})


@dataclass
class RequestTicket:
    fingerprint: str
    updated: float
    response: dict | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ConversationStore:
    """Opaque IDs are capabilities within this shared-token teaching application."""

    def __init__(self, ttl=3600, max_conversations=64, max_turns=6,
                 cache_ttl=900, max_cached_requests=32, clock=time.monotonic):
        self.ttl = ttl
        self.max_conversations = max_conversations
        self.max_turns = max_turns
        self.cache_ttl = cache_ttl
        self.max_cached_requests = max_cached_requests
        self.clock = clock
        self.conversations = OrderedDict()
        self.requests = OrderedDict()

    @staticmethod
    def scope_key(request):
        return json.dumps({"dataset_id": request.dataset_id, "as_of": request.as_of,
                           "filters": request.filters.model_dump(exclude_none=True)},
                          sort_keys=True, ensure_ascii=False)

    def _trim(self, values, ttl, limit):
        now = self.clock()
        for key, value in list(values.items()):
            if now - value.updated >= ttl and not value.lock.locked():
                del values[key]
        while len(values) >= limit:
            idle = next((key for key, value in values.items() if not value.lock.locked()), None)
            if idle is None:
                raise ConversationConflict("Все слоты анализа заняты. Повторите запрос позже.")
            del values[idle]

    def resolve(self, request):
        identifier = request.conversation_id
        key = self.scope_key(request)
        existing = self.conversations.get(identifier)
        if existing and existing.scope_key == key and (existing.lock.locked() or self.clock() - existing.updated < self.ttl):
            existing.updated = self.clock()
            self.conversations.move_to_end(identifier)
            return existing, False
        self._trim(self.conversations, self.ttl, self.max_conversations)
        value = Conversation(uuid.uuid4().hex, key, self.clock())
        self.conversations[value.identifier] = value
        return value, bool(identifier)

    def ticket(self, request):
        identifier = request.client_request_id
        if identifier is None:
            return RequestTicket("", self.clock())
        # Conversation identity belongs in the fingerprint: never reuse a turn
        # in another dialog, even if its visible question happens to match.
        payload = request.model_dump(exclude={"client_request_id"}, exclude_none=True)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        existing = self.requests.get(identifier)
        if existing and (existing.lock.locked() or self.clock() - existing.updated < self.cache_ttl):
            if existing.fingerprint != fingerprint:
                raise ConversationConflict("Повторный идентификатор запроса относится к другому вопросу. Начните новый анализ.")
            existing.updated = self.clock()
            self.requests.move_to_end(identifier)
            return existing
        self._trim(self.requests, self.cache_ttl, self.max_cached_requests)
        value = RequestTicket(fingerprint, self.clock())
        self.requests[identifier] = value
        return value

    def remember(self, conversation, question, result):
        conversation.turns.append({"question": question[:2500], "answer": result["answer"][:1200]})
        conversation.turns[:] = conversation.turns[-self.max_turns:]
        conversation.last_scope = deepcopy(result.get("resolved_scope") or result.get("scope") or {})
        conversation.updated = self.clock()

    def cache(self, ticket, response):
        ticket.updated = self.clock()
        ticket.response = deepcopy(response)
