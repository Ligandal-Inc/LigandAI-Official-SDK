# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Typed preparation and native-featurizer evidence; neither implies model accuracy."""
from __future__ import annotations

import csv
import hashlib
import io
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ligandai.types import FoldResult, MSAChain, MSAResult

Sha256 = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
ChainPolicy = Literal["receptor_homologs_required", "binder_query_only_allowed", "explicit_query_only_requested"]


class _Evidence(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class NativeMSAChain(_Evidence):
    chain_name: str = Field(min_length=1, strict=True)
    chain_id: Annotated[int, Field(strict=True, ge=0)] | None = None
    chain_index: Annotated[int, Field(strict=True, ge=0)] | None = None
    policy: ChainPolicy
    query_exact: bool = Field(strict=True)
    query_sha256: Sha256
    unique_non_gap_rows: PositiveInt
    shape: list[PositiveInt] = Field(min_length=2, max_length=2)
    tensor_sha256: Sha256

    @model_validator(mode="after")
    def _check_native_chain(self):
        if self.chain_id is None and self.chain_index is None:
            raise ValueError("native chain_id or chain_index is required")
        if not self.query_exact:
            raise ValueError("native query identity was not verified")
        if self.unique_non_gap_rows > self.shape[0]:
            raise ValueError("unique non-gap row count exceeds native tensor rows")
        if self.policy == "receptor_homologs_required" and self.unique_non_gap_rows < 2:
            raise ValueError("required receptor homologs were not consumed")
        return self


class NativeMSAWitness(_Evidence):
    record_id: str = Field(min_length=1, strict=True)
    chains: list[NativeMSAChain] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_chains(self):
        names = [c.chain_name for c in self.chains]
        indices = [c.chain_id if c.chain_id is not None else c.chain_index for c in self.chains]
        if len(set(names)) != len(names) or len(set(indices)) != len(indices):
            raise ValueError("duplicate native chain identity in one witness")
        return self


class NativeMSARun(_Evidence):
    status: Literal["completed"]
    engine: Literal["boltz", "protenix"]
    input_identity_sha256: Sha256
    input_sha256: Sha256
    guard_sha256: Sha256
    native_source_sha256: Sha256
    fresh_output: str = Field(min_length=1, strict=True)
    witnesses: list[NativeMSAWitness] = Field(min_length=1)

    @field_validator("fresh_output")
    @classmethod
    def _absolute_output(cls, value):
        if not value.startswith("/") or any(c in value for c in "\n\r\x00"):
            raise ValueError("fresh_output must identify the absolute worker-owned directory")
        return value


class NativeMSAReceipt(_Evidence):
    """Actual pre-model featurization evidence, before model MSA subsampling.

    Parsing validates the receipt contract, not the remote artifact's existence
    or biological quality. ``require_queries`` binds it to caller-held construct
    identities without exposing sequences in the receipt.
    """
    receipt_schema: Literal["ligandai.native_msa.v1"] = Field(alias="schema")
    evidence_stage: Literal["native_featurizer"]
    runs: list[NativeMSARun] = Field(min_length=1)

    def require_queries(self, queries: dict[str, str], *, record_id: str | None = None) -> None:
        expected = {name: hashlib.sha256(query.encode()).hexdigest() for name, query in queries.items()}
        witnesses = [w for r in self.runs for w in r.witnesses if record_id is None or w.record_id == record_id]
        if not witnesses:
            raise ValueError("requested native record has no witness")
        for witness in witnesses:
            actual = {c.chain_name: c for c in witness.chains}
            if set(actual) != set(expected):
                raise ValueError("native chain set differs from the requested protein chains")
            for name, digest in expected.items():
                if actual[name].query_sha256 != digest or actual[name].shape[1] != len(queries[name]):
                    raise ValueError(f"native construct identity differs for chain {name}")


class NativeMSAFoldResult(FoldResult):
    """Compatible FoldResult subclass with typed, explicitly optional evidence."""
    msa_consumption: NativeMSAReceipt | None = None

    @property
    def msa_evidence_stage(self) -> str:
        return "native_featurizer" if self.msa_consumption is not None else "unavailable"


def get_native_msa_receipt(payload: Any) -> NativeMSAReceipt | None:
    """Read both worker envelopes; reject conflicting copies instead of guessing."""
    if isinstance(payload, NativeMSAFoldResult):
        return payload.msa_consumption
    if isinstance(payload, NativeMSAReceipt):
        return payload
    pending, receipts, seen = [payload], [], set()
    while pending:
        node = pending.pop()
        if not isinstance(node, dict) or id(node) in seen:
            continue
        seen.add(id(node))
        for key in ("msa_consumption", "msaConsumption"):
            if node.get(key) is not None:
                receipts.append(NativeMSAReceipt.model_validate(node[key]))
        for key in ("result", "output_data", "outputData", "prediction_metadata", "predictionMetadata"):
            if isinstance(node.get(key), dict):
                pending.append(node[key])
    if not receipts:
        return None
    canonical = receipts[0].model_dump(mode="json", by_alias=True)
    if any(receipt.model_dump(mode="json", by_alias=True) != canonical for receipt in receipts[1:]):
        raise ValueError("conflicting native MSA receipts in response envelopes")
    return receipts[0]


class PreparedMSAReceipt(_Evidence):
    receipt_schema: Literal["ligandai.prepared_msa.v1"] = Field(default="ligandai.prepared_msa.v1", alias="schema")
    evidence_stage: Literal["prepared_alignment"] = "prepared_alignment"
    chain_name: str
    query_sha256: Sha256
    alignment_sha256: Sha256
    aligned_columns: PositiveInt
    rows: PositiveInt
    unique_non_gap_rows: PositiveInt
    native_consumption_verified: Literal[False] = False


class PreparedMSAChain(MSAChain):
    # The general SDK model strips string whitespace; alignment bytes are
    # identity-bearing and must retain their trailing newline verbatim.
    model_config = ConfigDict(str_strip_whitespace=False)
    prepared: PreparedMSAReceipt


class PreparedMSAResult(MSAResult):
    chains: dict[str, PreparedMSAChain]


def validate_prepared_msa(raw: Any, queries: dict[str, str]) -> PreparedMSAResult:
    """Check returned CSVs against exact requested queries without changing bytes."""
    result = MSAResult.model_validate(raw)
    if set(result.chains) != set(queries):
        raise ValueError("MSA service returned a different chain set than requested")
    prepared = {}
    alphabet = set("ACDEFGHIKLMNPQRSTVWYXBZUO-acdefghiklmnpqrstvwyxbzuo")
    for name, chain in result.chains.items():
        query = queries[name]
        csv_text = raw["chains"][name]["csv"] if isinstance(raw, dict) else chain.csv
        if not query or any(c not in "ACDEFGHIKLMNPQRSTVWYXBZUO" for c in query):
            raise ValueError("MSA queries must be exact uppercase ungapped sequences")
        reader = csv.reader(io.StringIO(csv_text, newline=""), strict=True)
        if next(reader, None) != ["key", "sequence"]:
            raise ValueError("MSA CSV requires key,sequence header")
        rows = list(reader)
        if not rows or len(rows[0]) != 2 or rows[0][1] != query:
            raise ValueError(f"MSA first query differs from requested chain {name}")
        aligned = []
        for row in rows:
            if len(row) != 2 or re.fullmatch(r"-?[0-9]+", row[0]) is None:
                raise ValueError("malformed MSA CSV row or pairing key")
            sequence = row[1]
            columns = "".join(c for c in sequence if not c.islower())
            if (not sequence or set(sequence) - alphabet or len(columns) != len(query)
                    or not columns.replace("-", "")):
                raise ValueError(f"invalid alignment columns for chain {name}")
            aligned.append(columns)
        receipt = PreparedMSAReceipt(chain_name=name,
            query_sha256=hashlib.sha256(query.encode()).hexdigest(),
            alignment_sha256=hashlib.sha256(csv_text.encode()).hexdigest(),
            aligned_columns=len(query), rows=len(rows), unique_non_gap_rows=len(set(aligned)))
        chain_data = chain.model_dump(exclude={"prepared"})
        chain_data["csv"] = csv_text
        prepared[name] = PreparedMSAChain(**chain_data, prepared=receipt)
    return PreparedMSAResult(**result.model_dump(exclude={"chains"}), chains=prepared)
