"""Deterministically resolve recorded linker placeholders from an exact query.

This pure contract module is shipped byte-identically in the SDK and worker.
It generates no residues and constrains no coordinates.
"""
from copy import deepcopy
import hashlib
import json

AA = frozenset("ACDEFGHIKLMNPQRSTVWY")
SCHEMA = "ligandai.linker_materialization.v1"


def _validate_canonical_json(value):
    """Require a JSON domain with identical Python/JavaScript canonical bytes.

    Float spellings (including 1.0 and -0.0) and integers beyond JS precision
    are intentionally unsupported. Metadata keys must be nonnumeric ASCII;
    this avoids UTF-16 sort and JavaScript integer-property ordering differences.
    """
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > 9007199254740991:
            raise ValueError("metadata integer exceeds the JavaScript safe range")
        return
    if type(value) is str:
        if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError("metadata contains an unpaired Unicode surrogate")
        return
    if type(value) is list:
        for item in value:
            _validate_canonical_json(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if (type(key) is not str or not key or not key.isascii()
                    or key.isdecimal()):
                raise ValueError("metadata keys must be nonnumeric ASCII strings")
            _validate_canonical_json(item)
        return
    raise ValueError("metadata must use JSON values with safe integers, never floats")


def metadata_sha256(value):
    _validate_canonical_json(value)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _materialize_source(sequence, source):
    if not isinstance(sequence, str) or not sequence or not set(sequence) <= AA:
        raise ValueError("materialization requires the unchanged exact uppercase full sequence")
    if not isinstance(source, dict):
        raise ValueError("source linker metadata must be an object")
    if "linker_template" in source or "linker_materialization" in source:
        raise ValueError("source metadata must be the original unmaterialized record")
    digest = hashlib.sha256(sequence.encode("ascii")).hexdigest()
    if source.get("sequence_sha256") is not None and source["sequence_sha256"] != digest:
        raise ValueError("source metadata hash differs from full sequence")
    novel = source.get("novel_length")
    if type(novel) is not int or novel <= 0:
        raise ValueError("materialization requires positive integer novel_length")
    parts, ends = {}, {}
    for role in ("linker", "tag"):
        part = source.get(role + "_residues")
        part = "" if part is None else part
        allowed = AA | {"-"} if role == "linker" else AA
        if not isinstance(part, str) or not set(part) <= allowed:
            raise ValueError("linker template or literal tag contains invalid characters")
        end = source.get(role + "_terminus")
        if (part and end not in ("N", "C")) or (end is not None and end not in ("N", "C")):
            raise ValueError("materialization requires explicit valid scaffold termini")
        parts[role], ends[role] = part, end
    template = parts["linker"]
    if not template or "-" not in template:
        raise ValueError("materialization requires a recorded linker template with placeholders")
    expected_origin = "generative" if set(template) == {"-"} else "mixed"
    if source.get("linker_origin") != expected_origin:
        raise ValueError("linker_origin contradicts the recorded fixed/generated template")
    prefix = [r for r in ("tag", "linker") if parts[r] and ends[r] == "N"]
    suffix = [r for r in ("linker", "tag") if parts[r] and ends[r] == "C"]
    if novel + sum(map(len, parts.values())) != len(sequence):
        raise ValueError("declared lengths do not tile the full sequence")
    offset, resolved, core_start = 0, None, None
    for role in prefix + ["core"] + suffix:
        width = novel if role == "core" else len(parts[role])
        if role == "core":
            core_start = offset
        else:
            actual = sequence[offset:offset + width]
            if any(letter != "-" and letter != observed for letter, observed in zip(parts[role], actual)):
                raise ValueError("fixed linker/tag letters differ from the exact full sequence")
            if role == "linker":
                resolved = actual
        offset += width
    for key, expected in (("binder_start", core_start), ("binder_end", core_start + novel)):
        if key in source and (type(source[key]) is not int or source[key] != expected):
            raise ValueError("source core boundaries disagree with declared sequence parts")
    output = deepcopy(source)
    output.update(linker_residues=resolved, linker_template=template, sequence_sha256=digest)
    output["linker_materialization"] = {
        "schema": SCHEMA, "source_metadata": deepcopy(source),
        "source_metadata_sha256": metadata_sha256(source), "sequence_sha256": digest,
    }
    return output


def validate_linker_materialization(sequence, metadata):
    """Return per-linker fixed flags, or None for legacy literal-only metadata."""
    if "linker_template" not in metadata and "linker_materialization" not in metadata:
        return None
    receipt = metadata.get("linker_materialization")
    if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA:
        raise ValueError("linker_template requires an exact materialization receipt")
    source = receipt.get("source_metadata")
    if metadata_sha256(source) != receipt.get("source_metadata_sha256"):
        raise ValueError("original linker metadata hash differs")
    expected = _materialize_source(sequence, source)
    if metadata != expected:
        raise ValueError("materialized metadata differs from exact original record and full sequence")
    return tuple(letter != "-" for letter in metadata["linker_template"])


def materialize_linker_metadata(sequence, metadata):
    """Explicit producer step; fold consumers must still reject raw templates."""
    if not isinstance(metadata, dict):
        raise ValueError("linker metadata must be an object")
    if "linker_template" in metadata or "linker_materialization" in metadata:
        validate_linker_materialization(sequence, metadata)
        return deepcopy(metadata)
    return _materialize_source(sequence, metadata)
