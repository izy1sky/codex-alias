"""Compatibility mappings for persisted Codex Responses items.

The wire shape is primarily a model/API concern, while encrypted payloads are
bound to the backend that produced them.  Keep both dimensions explicit here;
provider names are aliases and are deliberately not used as capability flags.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase
from typing import Callable


Record = dict[str, object]


class MappingAction(Enum):
    KEEP = "keep"
    REWRITE = "rewrite"
    DROP = "drop"


class MappingRisk(Enum):
    LOSSLESS = "lossless"
    LOSSY = "lossy"


@dataclass(frozen=True, slots=True)
class SessionMappingContext:
    """Facts used to decide whether a persisted item is portable."""

    source_model: str | None
    target_model: str | None
    source_provider: str | None
    target_provider: str
    source_backend_fingerprint: str | None = None
    target_backend_fingerprint: str | None = None
    source_cli_version: str | None = None
    target_wire_api: str | None = None

    @property
    def crosses_known_backends(self) -> bool:
        return (
            self.source_backend_fingerprint is not None
            and self.target_backend_fingerprint is not None
            and self.source_backend_fingerprint
            != self.target_backend_fingerprint
        )


RuleMapper = Callable[[Record, SessionMappingContext], MappingAction]


@dataclass(frozen=True, slots=True)
class SessionMappingRule:
    """One named transform with an explicit data-loss classification."""

    name: str
    risk: MappingRisk
    map_record: RuleMapper


@dataclass(frozen=True, slots=True)
class SessionMappingResult:
    records: tuple[Record, ...]
    mapped_records: int = 0
    rewritten_records: int = 0
    dropped_records: int = 0
    applied_mappings: tuple[str, ...] = ()
    lossy_mappings: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


def _payload(record: Record, item_type: str) -> dict[str, object] | None:
    if record.get("type") != "response_item":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != item_type:
        return None
    return payload


def _drop_foreign_encrypted_reasoning(
    record: Record, context: SessionMappingContext
) -> MappingAction:
    payload = _payload(record, "reasoning")
    if payload is None or not context.crosses_known_backends:
        return MappingAction.KEEP
    encrypted = payload.get("encrypted_content")
    if not isinstance(encrypted, str) or not encrypted:
        return MappingAction.KEEP
    # Reasoning ciphertext is backend-bound. Keeping it makes the target try to
    # decrypt an opaque foreign blob; dropping only this internal item retains
    # the public assistant answer and complete tool history.
    return MappingAction.DROP


def _empty_gpt5_reasoning_content(
    record: Record, context: SessionMappingContext
) -> MappingAction:
    payload = _payload(record, "reasoning")
    if payload is None or context.target_model is None:
        return MappingAction.KEEP
    model = context.target_model.strip().casefold()
    if not fnmatchcase(model, "gpt-5*"):
        return MappingAction.KEEP
    content = payload.get("content")
    if not isinstance(content, list) or not content:
        return MappingAction.KEEP
    payload["content"] = []
    return MappingAction.REWRITE


# Ordered from the stronger portability constraint to shape normalization.
# Add future behavior as another rule and state its loss classification.
SESSION_MAPPING_RULES: tuple[SessionMappingRule, ...] = (
    SessionMappingRule(
        name="foreign-backend-drop-encrypted-reasoning",
        risk=MappingRisk.LOSSY,
        map_record=_drop_foreign_encrypted_reasoning,
    ),
    SessionMappingRule(
        name="gpt-5-empty-reasoning-content",
        risk=MappingRisk.LOSSLESS,
        map_record=_empty_gpt5_reasoning_content,
    ),
)


def _contains_encrypted_payload(value: object) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"encrypted_content", "encrypted_payload"} and child:
                return True
            if _contains_encrypted_payload(child):
                return True
    elif isinstance(value, list):
        return any(_contains_encrypted_payload(child) for child in value)
    return False


def _diagnose_records(
    records: list[Record], context: SessionMappingContext
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    warnings: list[str] = []
    blockers: list[str] = []

    calls: dict[str, str] = {}
    outputs: dict[str, str] = {}
    incomplete: set[str] = set()
    encrypted_reasoning = 0
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        item_type = payload.get("type")
        if item_type == "reasoning" and payload.get("encrypted_content"):
            encrypted_reasoning += 1
        if (
            context.crosses_known_backends
            and item_type in {"compaction", "compacted"}
            and _contains_encrypted_payload(payload)
        ):
            blockers.append(
                "foreign encrypted compaction cannot be migrated safely; "
                "its plaintext history may no longer exist"
            )
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        if item_type in {"function_call", "custom_tool_call"}:
            calls[call_id] = str(item_type)
            if payload.get("status") == "incomplete":
                incomplete.add(call_id)
        elif item_type in {"function_call_output", "custom_tool_call_output"}:
            outputs[call_id] = str(item_type)

    orphan_calls = sorted(set(calls) - set(outputs))
    orphan_outputs = sorted(set(outputs) - set(calls))
    mismatched = sorted(
        call_id
        for call_id in set(calls) & set(outputs)
        if (calls[call_id].startswith("custom_"))
        != (outputs[call_id].startswith("custom_"))
    )
    if orphan_calls:
        warnings.append(
            f"{len(orphan_calls)} historical tool call(s) have no output"
        )
    if orphan_outputs:
        warnings.append(
            f"{len(orphan_outputs)} historical tool output(s) have no call"
        )
    if incomplete:
        warnings.append(
            f"{len(incomplete)} historical tool call(s) are marked incomplete"
        )
    if mismatched:
        warnings.append(
            f"{len(mismatched)} historical tool call/output pair(s) mix "
            "function and custom formats"
        )
    if (
        encrypted_reasoning
        and context.source_provider != context.target_provider
        and not context.crosses_known_backends
        and (
            context.source_backend_fingerprint is None
            or context.target_backend_fingerprint is None
        )
    ):
        warnings.append(
            f"could not verify portability of {encrypted_reasoning} encrypted "
            "reasoning item(s) because a backend fingerprint is missing; "
            "records were preserved"
        )

    return tuple(dict.fromkeys(warnings)), tuple(dict.fromkeys(blockers))


def apply_session_mappings(
    records: list[Record], context: SessionMappingContext
) -> SessionMappingResult:
    """Map a complete rollout and return changes plus safety diagnostics."""
    warnings, blockers = _diagnose_records(records, context)
    output: list[Record] = []
    mapped_records = 0
    rewritten_records = 0
    dropped_records = 0
    applied: set[str] = set()
    lossy: set[str] = set()

    for record in records:
        changed = False
        dropped = False
        for rule in SESSION_MAPPING_RULES:
            action = rule.map_record(record, context)
            if action is MappingAction.KEEP:
                continue
            changed = True
            applied.add(rule.name)
            if rule.risk is MappingRisk.LOSSY:
                lossy.add(rule.name)
            if action is MappingAction.DROP:
                dropped = True
                dropped_records += 1
                break
            rewritten_records += 1
        if changed:
            mapped_records += 1
        if not dropped:
            output.append(record)

    return SessionMappingResult(
        records=tuple(output),
        mapped_records=mapped_records,
        rewritten_records=rewritten_records,
        dropped_records=dropped_records,
        applied_mappings=tuple(sorted(applied)),
        lossy_mappings=tuple(sorted(lossy)),
        warnings=warnings,
        blockers=blockers,
    )
