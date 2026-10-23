"""
core/compaction.py - FP-09: Restorable Context Compaction with Pointer References.

Architectural Purpose:
Prevents quadratic prompt token bloat in long-running missions by batch-compacting
aging tool observations into concise digests paired with restorable Scratchpad references.

Key Capabilities:
1. Batched Compaction (Cache-Friendly):
   - Compaction runs in discrete batches rather than continuously modifying history turn-by-turn,
     minimizing prefix cache invalidation (protecting FP-06 prefix stability).
2. Immutable Critical Zones:
   - System prompts, policy rules, human approval audit entries, and provenance metadata
     are strictly quarantined and NEVER compacted.
3. Trust Inheritance (Zero Injection Laundering):
   - Compacting untrusted observations produces digests that strictly inherit the lowest trust
     level of their sources ('untrusted'). An attacker cannot launder prompt injection by having
     their instructions compressed into a summary.
4. Restorable Raw Store:
   - Full raw observations are archived in FP-07 Scratchpad before compaction, allowing
     exact restoration if an agent requires complete historical evidence.
"""

from dataclasses import dataclass, field
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.scratchpad import ScratchpadPointer, ScratchpadStore

logger = logging.getLogger("neural_swarm.compaction")


# Immutable message roles that can never be modified or compacted
IMMUTABLE_ROLES: Set[str] = {"system", "approval", "policy"}


@dataclass
class CompactionResult:
    """Summary of compaction run with token/byte efficiency metrics."""
    messages: List[Dict[str, Any]]
    compacted_count: int
    raw_bytes_before: int
    compacted_bytes_after: int
    saved_bytes: int
    pointers_created: List[ScratchpadPointer] = field(default_factory=list)

    @property
    def reduction_percentage(self) -> float:
        if self.raw_bytes_before <= 0:
            return 0.0
        return round((self.saved_bytes / self.raw_bytes_before) * 100.0, 2)


class ContextCompactor:
    """
    Manages restorable context compaction using scratchpad reference pointers.
    """

    def __init__(
        self,
        scratchpad_store: ScratchpadStore,
        compaction_strategy: str = "digest",  # "digest", "truncation", "none"
        observation_size_threshold: int = 500,  # bytes
    ) -> None:
        self.scratchpad = scratchpad_store
        self.compaction_strategy = compaction_strategy
        self.observation_size_threshold = observation_size_threshold

    def is_compactable(self, message: Dict[str, Any]) -> bool:
        """Verify if a message is eligible for compaction under security constraints."""
        role = message.get("role", "")
        if role in IMMUTABLE_ROLES:
            return False

        # Do not compact human approvals or policy declarations
        if message.get("category") in ("approval", "provenance", "policy"):
            return False

        # Only compact tool observations or bulky assistant reasoning
        if role in ("tool", "assistant", "observation"):
            content = str(message.get("content", ""))
            return len(content.encode("utf-8")) > self.observation_size_threshold

        return False

    def compact_messages(
        self,
        tenant_id: str,
        messages: List[Dict[str, Any]],
        keep_last_n_turns: int = 2,
    ) -> CompactionResult:
        """
        Batch-compact older observations while keeping recent turns and system prompts untouched.
        Replaces raw content with a digest and restorable scratchpad pointer.
        """
        if self.compaction_strategy == "none" or not messages:
            return CompactionResult(
                messages=list(messages),
                compacted_count=0,
                raw_bytes_before=0,
                compacted_bytes_after=0,
                saved_bytes=0,
            )

        compacted_list: List[Dict[str, Any]] = []
        pointers: List[ScratchpadPointer] = []
        raw_bytes_total = 0
        compacted_bytes_total = 0
        compacted_count = 0

        # Protect the last N messages from compaction so active deliberation context is intact
        split_idx = max(0, len(messages) - keep_last_n_turns)
        older_messages = messages[:split_idx]
        recent_messages = messages[split_idx:]

        for msg in older_messages:
            msg_copy = dict(msg)
            content_str = str(msg_copy.get("content", ""))
            content_bytes = len(content_str.encode("utf-8"))
            raw_bytes_total += content_bytes

            if self.is_compactable(msg_copy):
                # 1. Inherit trust: if source was untrusted or tool_untrusted, digest remains untrusted
                original_trust = msg_copy.get("trust_label") or (
                    "untrusted" if msg_copy.get("role") == "tool" else "internal"
                )

                # 2. Archive raw payload into Scratchpad
                pointer = self.scratchpad.store(
                    tenant_id=tenant_id,
                    content=content_str,
                    trust_label=original_trust,
                    custom_summary=f"Compacted observation for role={msg_copy.get('role')} name={msg_copy.get('name')}",
                )
                pointers.append(pointer)

                # 3. Create digest replacement
                if self.compaction_strategy == "truncation":
                    digest = content_str[:150] + f"... [Truncated {content_bytes - 150} bytes]"
                else:  # "digest"
                    lines = content_str.splitlines()
                    head = lines[0][:100] if lines else ""
                    digest = f"Observation Digest ({len(lines)} lines, {content_bytes} bytes). Head: {head}"

                # 4. Format compacted message with restorable pointer
                compacted_content = (
                    f"{digest}\n"
                    f"[RESTORE_POINTER ref_id={pointer.ref_id} trust={original_trust}]\n"
                    f"{pointer.to_reference_tag()}"
                )
                msg_copy["content"] = compacted_content
                msg_copy["compacted"] = True
                msg_copy["scratchpad_ref_id"] = pointer.ref_id
                msg_copy["trust_label"] = original_trust  # Preserves untrusted taint

                compacted_count += 1
                compacted_bytes = len(compacted_content.encode("utf-8"))
                compacted_bytes_total += compacted_bytes
            else:
                compacted_bytes_total += content_bytes

            compacted_list.append(msg_copy)

        # Append recent turns uncompacted
        for msg in recent_messages:
            c_bytes = len(str(msg.get("content", "")).encode("utf-8"))
            raw_bytes_total += c_bytes
            compacted_bytes_total += c_bytes
            compacted_list.append(dict(msg))

        saved = max(0, raw_bytes_total - compacted_bytes_total)

        logger.info(
            "[COMPACTION] Compacted %d messages for tenant '%s': saved %d bytes (%d -> %d).",
            compacted_count,
            tenant_id,
            saved,
            raw_bytes_total,
            compacted_bytes_total,
        )

        return CompactionResult(
            messages=compacted_list,
            compacted_count=compacted_count,
            raw_bytes_before=raw_bytes_total,
            compacted_bytes_after=compacted_bytes_total,
            saved_bytes=saved,
            pointers_created=pointers,
        )

    def restore_message(self, tenant_id: str, message: Dict[str, Any]) -> Dict[str, Any]:
        """
        Restore a compacted message back to its original raw form using the scratchpad pointer.
        """
        ref_id = message.get("scratchpad_ref_id")
        if not ref_id:
            return message

        item = self.scratchpad.dereference(tenant_id, ref_id)
        restored = dict(message)
        restored["content"] = item.content
        restored["compacted"] = False
        restored["trust_label"] = item.trust_label
        return restored
