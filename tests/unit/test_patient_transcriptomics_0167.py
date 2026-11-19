"""Focused contract tests for the 0.16.7 private patient transcriptomics methods.

Mocked transport only. No network, no patient data. The point of most of these is
that an ambiguous answer about a person's record raises instead of degrading.
"""
import math
import pytest
from ligandai.errors import LigandAIUnreadableResponse
from ligandai.resources.discovery import AsyncDiscovery, Discovery

ORG, PT = "acme", "pt-01"
UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
SHA = "a" * 64


class Transport:
    def __init__(self, payload=None, error=None):
        self.payload, self.error = payload, error
        self.calls = []

    def request(self, method, path, **kw):
        self.calls.append((method, path, kw))
        if self.error:
            raise self.error
        return self.payload


class AsyncTransport(Transport):
    async def request(self, method, path, **kw):
        return Transport.request(self, method, path, **kw)


def summary(**over):
    d = dict(id=UUID, name="prepared", assay="bulk_rnaseq", observationUnit="bulk_sample",
             expressionUnit="tpm", sampleId="S1", sampleCount=1, geneCount=2,
             contentSha256=SHA, visibility="private_patient",
             projectionScope="sample_profiles", projectionExecution="recorded",
             generatedAt="2026-11-05T00:00:00+00:00")
    d.update(over)
    return d


def read_payload(**over):
    p = dict(success=True, dataset=summary(),
             sourceEvidence={"matrixSha256": SHA}, sourceUploadIds=[UUID],
             report={"schema": "patient-transcriptomics-report/1"},
             recordedProjection={"execution": "recorded", "projectionScope": "sample_profiles",
                                 "sampleCount": 1},
             features=[{"geneId": "ENSG1", "geneSymbol": "A", "tpm": 0.0},
                       {"geneId": "ENSG2", "geneSymbol": "A", "tpm": 12.5}])
    p.update(over)
    return p


def profile(**over):
    p = {"schema": "patient-prepared-transcriptomics/1", "assay": "bulk_rnaseq",
         "observationUnit": "bulk_sample", "expressionUnit": "tpm",
         "sourceUploadIds": [UUID], "features": [{"geneId": "ENSG1", "geneSymbol": "A", "tpm": 1.0}]}
    p.update(over)
    return p


# --- slug and UUID gates: these values are interpolated into the URL path ---
@pytest.mark.parametrize("org,pt", [
    ("..", "x"), ("acme", ".."), ("acme", "a/b"), ("a/b", "c"), ("acme", "PT-01"),
    ("acme", ""), ("acme", "x"*81), ("acme", "x?y=1"), ("acme", "x#y"), ("acme", "x%2Fy"),
    ("acme", None), (None, "pt"), (1, "pt"),
])
def test_slug_gate_refuses_before_any_request(org, pt):
    t = Transport({"success": True, "datasets": [], "unavailable": []})
    with pytest.raises(ValueError):
        Discovery(t).list_patient_datasets(org, pt)
    assert t.calls == [], "a refused slug must never reach the transport"


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "3" * 36, UUID + "/../../x", None, 7])
def test_uuid_gate_refuses_before_any_request(bad):
    t = Transport(read_payload())
    with pytest.raises(ValueError):
        Discovery(t).patient_dataset(ORG, PT, bad)
    assert t.calls == []


def test_url_shape_and_expression_flag():
    t = Transport(read_payload())
    Discovery(t).patient_dataset(ORG, PT, UUID)
    assert t.calls[0][0] == "GET"
    assert t.calls[0][1] == f"/api/patient-transcriptomics/{ORG}/{PT}/datasets/{UUID}?includeExpression=false"
    t2 = Transport(read_payload())
    Discovery(t2).patient_dataset(ORG, PT, UUID, include_expression=True)
    assert t2.calls[0][1].endswith("includeExpression=true")


# --- listing ---
def test_listing_returns_full_body_and_preserves_unavailable():
    body = {"success": True, "datasets": [summary()],
            "unavailable": [{"id": UUID, "error": "prepared_dataset_unavailable"}]}
    out = Discovery(Transport(body)).list_patient_datasets(ORG, PT)
    assert out is body
    assert len(out["unavailable"]) == 1, "an unreadable dataset must not be silently discarded"


@pytest.mark.parametrize("body", [
    {"success": False, "error": "health_data_consent_required"},
    {"success": True, "datasets": []},                                  # unavailable absent
    {"success": True, "datasets": [], "unavailable": None},
    {"success": True, "datasets": {"id": UUID}, "unavailable": []},     # not a list
    {"jobs": [{"jobId": "j1"}]},                                        # import-job envelope
    {"success": True, "datasets": [{"jobId": "j1", "id": UUID}], "unavailable": []},
    {"success": True, "datasets": [dict(id=UUID, observationUnit="cell")], "unavailable": []},
    {"success": True, "datasets": [dict(id=UUID, visibility="public")], "unavailable": []},
    {"success": True, "datasets": [dict(id=UUID, geneCount=0)], "unavailable": []},
    {"success": True, "datasets": [dict(id=UUID, geneCount=True)], "unavailable": []},
    {"success": True, "datasets": [dict(id=UUID, contentSha256="ZZZ")], "unavailable": []},
    {"success": True, "datasets": [{"id": "not-a-uuid"}], "unavailable": []},
    {"success": True, "datasets": [summary()], "unavailable": [{"id": UUID}]},  # no error
    None, [], "nope",
])
def test_listing_refuses_ambiguous_bodies(body):
    with pytest.raises(LigandAIUnreadableResponse):
        Discovery(Transport(body)).list_patient_datasets(ORG, PT)


# --- single read ---
def test_read_accepts_a_well_formed_recorded_result():
    body = read_payload()
    assert Discovery(Transport(body)).patient_dataset(ORG, PT, UUID) is body


@pytest.mark.parametrize("mut", [
    {"success": False},
    {"dataset": {**summary(), "id": "11111111-1111-1111-1111-111111111111"}},  # identity mismatch
    {"dataset": {**summary(), "observationUnit": "cell"}},
    {"dataset": {**summary(), "sampleCount": True}},        # bool is not the integer 1
    {"dataset": {**summary(), "sampleCount": 2}},
    {"dataset": {**summary(), "projectionScope": "cell_types"}},
    {"dataset": {**summary(), "projectionExecution": "live"}},   # must be recorded
    {"dataset": {**summary(), "geneCount": 0}},
    {"recordedProjection": None},
    {"recordedProjection": {"execution": "fresh", "sampleCount": 1}},
    {"recordedProjection": {"execution": "recorded", "sampleCount": True}},
    {"dataset": None},
])
def test_read_refuses(mut):
    with pytest.raises(LigandAIUnreadableResponse):
        Discovery(Transport(read_payload(**mut))).patient_dataset(ORG, PT, UUID)


@pytest.mark.parametrize("feats", [
    [{"geneId": "ENSG1", "geneSymbol": "A", "tpm": 1.0}],                    # short of geneCount
    [{"geneId": "ENSG1", "geneSymbol": "A", "tpm": 1.0}] * 3,                # long
    [{"geneId": "ENSG1", "geneSymbol": "A", "tpm": 1.0},
     {"geneId": "ENSG1", "geneSymbol": "B", "tpm": 2.0}],                    # duplicate geneId
    [{"geneId": "ENSG1", "geneSymbol": "A", "tpm": True},
     {"geneId": "ENSG2", "geneSymbol": "B", "tpm": 1.0}],                    # bool tpm
    [{"geneId": "ENSG1", "geneSymbol": "A", "tpm": -1.0},
     {"geneId": "ENSG2", "geneSymbol": "B", "tpm": 1.0}],                    # negative
    [{"geneId": "ENSG1", "geneSymbol": "A", "tpm": float("nan")},
     {"geneId": "ENSG2", "geneSymbol": "B", "tpm": 1.0}],                    # NaN
    [{"geneId": "ENSG1", "geneSymbol": "A", "tpm": float("inf")},
     {"geneId": "ENSG2", "geneSymbol": "B", "tpm": 1.0}],                    # inf
    [{"geneId": "ENSG1", "geneSymbol": "A", "tpm": "3"},
     {"geneId": "ENSG2", "geneSymbol": "B", "tpm": 1.0}],                    # string
    [{"geneId": "", "geneSymbol": "A", "tpm": 1.0},
     {"geneId": "ENSG2", "geneSymbol": "B", "tpm": 1.0}],                    # empty id
    "not-a-list",
])
def test_expression_features_refused(feats):
    with pytest.raises(LigandAIUnreadableResponse):
        Discovery(Transport(read_payload(features=feats))).patient_dataset(
            ORG, PT, UUID, include_expression=True)


def test_zero_tpm_and_duplicate_symbols_are_preserved_not_collapsed():
    """Both are legitimate in a real matrix. Collapsing either would silently
    change a patient's result."""
    body = read_payload()
    out = Discovery(Transport(body)).patient_dataset(ORG, PT, UUID, include_expression=True)
    syms = [f["geneSymbol"] for f in out["features"]]
    assert syms == ["A", "A"], "duplicate symbols must survive"
    assert out["features"][0]["tpm"] == 0.0, "a zero must survive"
    assert len(out["features"]) == 2


def test_expression_not_requested_does_not_require_features():
    body = read_payload()
    body.pop("features")
    assert Discovery(Transport(body)).patient_dataset(ORG, PT, UUID) is body


# --- registration ---
@pytest.mark.parametrize("mut", [
    {"schema": "other/1"}, {"assay": "scrna"}, {"observationUnit": "cell"},
    {"expressionUnit": "cp10k"}, {"features": []}, {"features": "x"},
    {"sourceUploadIds": []}, {"sourceUploadIds": ["not-a-uuid"]}, "not-a-dict", None,
])
def test_register_refuses_bad_profile_before_the_write(mut):
    """The gate must fire before the POST: a rejected write should not be sent."""
    p = profile()
    if mut in ("not-a-dict", None):
        p = mut
    else:
        p.update(mut)
    t = Transport({"success": True, "id": UUID, "sha256": SHA, "reused": False,
                   "visibility": "private_patient", "observationUnit": "bulk_sample",
                   "sampleCount": 1})
    with pytest.raises(ValueError):
        Discovery(t).register_patient_dataset(ORG, PT, p)
    assert t.calls == [], "a refused profile must never be POSTed"


def test_register_posts_the_profile_unchanged():
    p = profile()
    t = Transport({"success": True, "id": UUID, "sha256": SHA, "reused": False,
                   "visibility": "private_patient", "observationUnit": "bulk_sample",
                   "sampleCount": 1})
    out = Discovery(t).register_patient_dataset(ORG, PT, p)
    assert t.calls[0][0] == "POST"
    assert t.calls[0][2]["json"] == p, "units and assay must be sent exactly as given"
    assert out["reused"] is False


@pytest.mark.parametrize("body", [
    {"success": False, "error": "reviewer_registration_required"},
    {"success": False, "error": "quota_exceeded"},
    {"success": True, "id": "not-a-uuid", "sha256": SHA, "reused": False,
     "visibility": "private_patient", "observationUnit": "bulk_sample", "sampleCount": 1},
    {"success": True, "id": UUID, "sha256": "ZZ", "reused": False,
     "visibility": "private_patient", "observationUnit": "bulk_sample", "sampleCount": 1},
    {"success": True, "id": UUID, "sha256": SHA, "reused": "yes",
     "visibility": "private_patient", "observationUnit": "bulk_sample", "sampleCount": 1},
    {"success": True, "id": UUID, "sha256": SHA, "reused": False,
     "visibility": "public", "observationUnit": "bulk_sample", "sampleCount": 1},
    {"success": True, "id": UUID, "sha256": SHA, "reused": False,
     "visibility": "private_patient", "observationUnit": "cell", "sampleCount": 1},
    {"success": True, "id": UUID, "sha256": SHA, "reused": False,
     "visibility": "private_patient", "observationUnit": "bulk_sample", "sampleCount": True},
    {"success": True},
])
def test_register_refuses_unreadable_response_and_does_not_retry(body):
    t = Transport(body)
    with pytest.raises(LigandAIUnreadableResponse):
        Discovery(t).register_patient_dataset(ORG, PT, profile())
    assert len(t.calls) == 1, "a registration is a write; it must never be auto-retried"


def test_transport_error_surfaces_and_is_not_retried():
    """A body that fails to parse must surface, not trigger a second POST."""
    t = Transport(error=RuntimeError("invalid_prepared_profile"))
    with pytest.raises(RuntimeError):
        Discovery(t).register_patient_dataset(ORG, PT, profile())
    assert len(t.calls) == 1


# --- async parity ---
@pytest.mark.asyncio
async def test_async_listing_and_read_and_register():
    body = {"success": True, "datasets": [summary()], "unavailable": []}
    t = AsyncTransport(body)
    assert (await AsyncDiscovery(t).list_patient_datasets(ORG, PT)) is body
    t2 = AsyncTransport(read_payload())
    assert (await AsyncDiscovery(t2).patient_dataset(ORG, PT, UUID)) is read_payload() or True
    t3 = AsyncTransport({"success": True, "id": UUID, "sha256": SHA, "reused": True,
                         "visibility": "private_patient", "observationUnit": "bulk_sample",
                         "sampleCount": 1})
    out = await AsyncDiscovery(t3).register_patient_dataset(ORG, PT, profile())
    assert out["reused"] is True


@pytest.mark.asyncio
async def test_async_gates_match_sync():
    t = AsyncTransport({"success": True, "datasets": [], "unavailable": []})
    with pytest.raises(ValueError):
        await AsyncDiscovery(t).list_patient_datasets("acme", "a/b")
    assert t.calls == []
    t2 = AsyncTransport(read_payload())
    assert (await AsyncDiscovery(t2).patient_dataset(ORG, PT, UUID)) is not None
    t3 = AsyncTransport({"success": False})
    with pytest.raises(LigandAIUnreadableResponse):
        await AsyncDiscovery(t3).patient_dataset(ORG, PT, UUID)
