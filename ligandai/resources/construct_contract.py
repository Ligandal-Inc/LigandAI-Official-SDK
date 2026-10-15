"""Exact sequence-space boundaries for the existing linker_metadata contract.

These masks describe sequence roles and generation-time fixed residues. They
do not constrain coordinates or model a BLI sensor attachment.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ligandai.resources.msa_contract import NativeMSAFoldResult, Sha256
from ligandai.resources.linker_materialization import materialize_linker_metadata, validate_linker_materialization

_AA = frozenset("ACDEFGHIKLMNPQRSTVWY")


@dataclass(frozen=True)
class ConstructRegion:
    role: Literal["core", "linker", "tag"]
    start: int
    end: int
    fixed_sequence: bool | None
    fixed_sequence_mask: tuple[bool, ...] | None = None


@dataclass(frozen=True)
class FoldConstructBoundary:
    sequence: str
    sequence_sha256: str
    regions: tuple[ConstructRegion, ...]

    def mask(self, role: Literal["core", "linker", "tag"]) -> tuple[bool, ...]:
        if role not in ("core", "linker", "tag"):
            raise ValueError("construct role must be core, linker or tag")
        return tuple(any(r.role == role and r.start <= i < r.end for r in self.regions)
                     for i in range(len(self.sequence)))

    @property
    def fixed_sequence_mask(self) -> tuple[bool | None, ...]:
        return tuple(next(r.fixed_sequence_mask[i-r.start] if r.fixed_sequence_mask is not None else r.fixed_sequence
                          for r in self.regions if r.start <= i < r.end)
                     for i in range(len(self.sequence)))


def resolve_linker_metadata(sequence: str, metadata: dict[str, Any]) -> FoldConstructBoundary:
    """Resolve literal exact regions; reject placeholders or ambiguous identity.

    Indices are zero-based and half-open in the WHOLE folded chain. Each
    nonempty scaffold part needs an explicit N/C terminus; there is no guessed
    attachment end. ``novel_length`` pins the core length independently.
    """
    if not isinstance(sequence, str) or not sequence or set(sequence) - _AA:
        raise ValueError("fold construct must be an exact uppercase amino-acid sequence")
    if not isinstance(metadata, dict):
        raise ValueError("linker_metadata must be an object")
    digest = sha256(sequence.encode()).hexdigest()
    if metadata.get("sequence_sha256") is not None and metadata["sequence_sha256"] != digest:
        raise ValueError("linker_metadata sequence_sha256 differs from the full folded chain")
    novel = metadata.get("novel_length")
    if type(novel) is not int or novel < 1:
        raise ValueError("linker_metadata requires an explicit positive integer novel_length")
    parts = {}
    ends = {}
    for role in ("tag", "linker"):
        part = metadata.get(role + "_residues")
        part = "" if part is None else part
        if not isinstance(part, str) or set(part) - _AA:
            raise ValueError("fold linker/tag metadata requires literal uppercase residues, not placeholders")
        end = metadata.get(role + "_terminus")
        if part and end not in ("N", "C"):
            raise ValueError(f"nonempty {role} requires an explicit N or C terminus")
        if end is not None and end not in ("N", "C"):
            raise ValueError(f"invalid {role} terminus")
        parts[role], ends[role] = part, end
    origin = metadata.get("linker_origin")
    if origin not in (None, "fixed", "generative", "mixed"):
        raise ValueError("invalid linker_origin")
    linker_fixed = validate_linker_materialization(sequence, metadata)
    prefix_roles = [r for r in ("tag", "linker") if parts[r] and ends[r] == "N"]
    suffix_roles = [r for r in ("linker", "tag") if parts[r] and ends[r] == "C"]
    prefix = "".join(parts[r] for r in prefix_roles)
    suffix = "".join(parts[r] for r in suffix_roles)
    if len(prefix) + novel + len(suffix) != len(sequence):
        raise ValueError("novel_length and scaffold lengths do not tile the full construct")
    if not sequence.startswith(prefix) or not sequence.endswith(suffix):
        raise ValueError("literal linker/tag residues do not match their declared ends")
    core_start, core_end = len(prefix), len(prefix) + novel
    # Optional explicit frame bounds must agree, never override literal parts.
    for key, expected in (("binder_start", core_start), ("binder_end", core_end)):
        if key in metadata and (type(metadata[key]) is not int or metadata[key] != expected):
            raise ValueError(f"{key} differs from exact construct boundaries")
    fixed = {"core": False, "tag": True,
             "linker": True if origin == "fixed" else False if origin == "generative" else None}
    regions = []
    cursor = 0
    for role in prefix_roles + ["core"] + suffix_roles:
        width = novel if role == "core" else len(parts[role])
        regions.append(ConstructRegion(role, cursor, cursor + width, fixed[role],
                                       linker_fixed if role == "linker" else None))
        cursor += width
    return FoldConstructBoundary(sequence, digest, tuple(regions))


def fold_linker_metadata(entity: dict[str, Any], sequence: str) -> dict[str, Any] | None:
    """Validate aliases and return the existing wire field with full-query SHA."""
    candidates = [entity[k] for k in ("linker_metadata", "linkerMetadata") if entity.get(k) is not None]
    if not candidates:
        return None
    checked = []
    for raw in candidates:
        if isinstance(raw, dict) and isinstance(raw.get("linker_residues"), str) and "-" in raw["linker_residues"]:
            raw = materialize_linker_metadata(sequence, raw)
        boundary = resolve_linker_metadata(sequence, raw)
        if entity.get("sequence_sha256") is not None and entity["sequence_sha256"] != boundary.sequence_sha256:
            raise ValueError("entity sequence_sha256 differs from the full folded chain")
        checked.append({**raw, "sequence_sha256": boundary.sequence_sha256})
    if len(checked) == 2 and checked[0] != checked[1]:
        raise ValueError("conflicting linker_metadata and linkerMetadata aliases")
    return checked[0]


class ConstructChainScores(BaseModel):
    """Worker-observed post-fold regions; absent scientific metrics stay absent."""
    model_config = ConfigDict(extra="allow")
    status: Literal["scored", "unavailable", "unsupported"]
    reason: str | None = None
    sequence_sha256: Sha256 | None = None
    sequence_length: int | None = Field(default=None, strict=True, gt=0)
    coordinate_constrained: bool | None = Field(default=None, strict=True)
    regions: list[dict[str, Any]] = Field(default_factory=list)
    core: dict[str, Any] | None = None
    linker: dict[str, Any] | None = None
    tag: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _consistent_regions(self):
        if self.coordinate_constrained not in (None, False):
            raise ValueError("region metrics do not attest coordinate constraints")
        if self.status != "scored":
            if not self.reason or any(value is not None for value in (self.core, self.linker, self.tag)):
                raise ValueError("unavailable region scores need a reason and null metrics")
        elif self.reason is not None or not self.regions:
            raise ValueError("scored regions require exact boundaries and no error reason")
        if self.regions:
            if self.sequence_sha256 is None or self.sequence_length is None or self.coordinate_constrained is not False:
                raise ValueError("region boundaries require full sequence identity and scope")
            cursor, roles = 0, set()
            for region in self.regions:
                role, start, end = (region.get(k) for k in ("role", "start", "end"))
                if (role not in ("core", "linker", "tag") or role in roles or type(start) is not int
                        or type(end) is not int or start != cursor or end <= start
                        or end > self.sequence_length or "fixed_sequence" not in region
                        or (region["fixed_sequence"] is not None and type(region["fixed_sequence"]) is not bool)):
                    raise ValueError("region bounds must tile the exact full sequence once")
                if self.status == "scored" and getattr(self, role) is None:
                    raise ValueError("scored region is missing its measured metrics")
                cursor = end
                roles.add(role)
            if cursor != self.sequence_length or "core" not in roles:
                raise ValueError("region boundaries are incomplete")
            core = next(region for region in self.regions if region["role"] == "core")
            expected_binder = [core["start"] <= i < core["end"] for i in range(self.sequence_length)]
            extras = self.model_extra or {}
            expected_fixed = []
            for region in self.regions:
                width = region["end"] - region["start"]
                specific = region.get("fixed_sequence_mask")
                if specific is not None:
                    template = extras.get("linker_template")
                    source_hash = extras.get("source_metadata_sha256")
                    if (region["role"] != "linker" or not isinstance(specific, list)
                            or len(specific) != width or any(type(v) is not bool for v in specific)
                            or not isinstance(template, str) or len(template) != width
                            or set(template) - (_AA | {"-"}) or "-" not in template
                            or specific != [letter != "-" for letter in template]
                            or region["fixed_sequence"] is not (False if not any(specific) else None)
                            or not isinstance(source_hash, str) or len(source_hash) != 64
                            or set(source_hash) - set("0123456789abcdef")):
                        raise ValueError("per-residue fixed mask needs exact linker template provenance")
                    expected_fixed.extend(specific)
                else:
                    expected_fixed.extend([region["fixed_sequence"]] * width)
            for key, expected in (("binder_start", core["start"]), ("binder_end", core["end"])):
                if key in extras and (type(extras[key]) is not int or extras[key] != expected):
                    raise ValueError("region receipt carries conflicting core bounds")
            for key, expected in (("binder_mask", expected_binder), ("fixed_sequence_mask", expected_fixed)):
                if key in extras and (not isinstance(extras[key], list) or extras[key] != expected
                        or any(value is not None and type(value) is not bool for value in extras[key])):
                    raise ValueError("region receipt mask differs from exact ranges")
            for role in ("core", "linker", "tag"):
                if role not in roles and getattr(self, role) is not None:
                    raise ValueError("absent region cannot carry measured scores")
        return self


class ConstructRegionScores(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    receipt_schema: Literal["ligandai.construct_regions.v1"] = Field(alias="schema")
    score_scope: Literal["post_fold_region_recalculation"]
    chains: dict[str, ConstructChainScores]

    def require_query(self, chain_id: str, sequence: str) -> None:
        chain = self.chains.get(chain_id)
        if (chain is None or chain.sequence_sha256 != sha256(sequence.encode()).hexdigest()
                or chain.sequence_length != len(sequence)):
            raise ValueError("construct region receipt differs from exact full-chain identity")


class ConstructFoldResult(NativeMSAFoldResult):
    construct_regions: ConstructRegionScores | None = None


def get_construct_regions(payload: Any) -> ConstructRegionScores | None:
    """Preserve the worker's named post-fold receipt across status envelopes."""
    if isinstance(payload, ConstructFoldResult):
        return payload.construct_regions
    pending, receipts, seen = [payload], [], set()
    while pending:
        node = pending.pop()
        if not isinstance(node, dict) or id(node) in seen:
            continue
        seen.add(id(node))
        if node.get("construct_regions") is not None:
            receipts.append(ConstructRegionScores.model_validate(node["construct_regions"]))
        for key in ("result", "output_data", "outputData", "ipsae_scores", "prediction_metadata"):
            if isinstance(node.get(key), dict):
                pending.append(node[key])
    if not receipts:
        return None
    if any(receipt.model_dump() != receipts[0].model_dump() for receipt in receipts[1:]):
        raise ValueError("conflicting construct region receipts")
    return receipts[0]
