# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""UniProt info, variants, and custom protein uploads."""

from __future__ import annotations

from pathlib import Path

from ligandai.errors import (
    LigandAILegalAcceptanceRequired,
    resolve_mutation_failure,
)
from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import (
    DisorderProfile,
    ProteinInfo,
    ProteinVariant,
    ReceptorAtlas,
    ReceptorProfile,
    ReceptorIntelligence,
    ReceptorTopology,
    UserProtein,
)


class Proteins(Resource):
    """Protein info, receptor topology, variants, and custom protein uploads."""

    def info(
        self,
        query: str,
        include_sequence: bool = True,
        include_ptms: bool = True,
        include_domains: bool = True,
    ) -> ProteinInfo:
        params = {
            "include_sequence": include_sequence,
            "include_ptms": include_ptms,
            "include_domains": include_domains,
        }
        return ProteinInfo.model_validate(
            self._transport.request("GET", f"/api/protein-info/{query}", params=params)
            or {"gene": query}
        )

    def disorder_profile(self, gene: str) -> DisorderProfile:
        return DisorderProfile.model_validate(
            self._transport.request("GET", f"/api/protein-info/disorder/{gene}") or {"gene": gene}
        )

    def receptor_topology(self, gene: str) -> ReceptorTopology:
        return ReceptorTopology.model_validate(
            self._transport.request("GET", f"/api/receptor-topology/{gene}") or {"gene": gene}
        )

    def receptor_intelligence(
        self,
        gene: str | None = None,
        genes: list[str] | None = None,
    ) -> ReceptorIntelligence | dict[str, ReceptorIntelligence]:
        if genes is not None:
            payload = self._transport.request(
                "POST", "/api/receptor-intelligence/batch", json={"genes": genes}
            ) or {}
            results = payload.get("results", payload)
            return {g: ReceptorIntelligence.model_validate(v) for g, v in results.items()}
        if gene is None:
            raise ValueError("Pass gene= or genes=")
        return ReceptorIntelligence.model_validate(
            self._transport.request("GET", f"/api/receptor-intelligence/{gene}") or {"gene": gene}
        )

    def receptor_profile(self, gene: str) -> ReceptorProfile:
        """Decision-oriented Receptor Intelligence profile for ``gene``.

        ``GET /api/receptor-intelligence/profile/{gene}`` — requires an
        **academia / pro / enterprise** plan or higher.

        Use this to guide design rather than to read the atlas. It answers the two
        questions the raw blocks don't:

        * *Should I design against this target, and how?* → ``target_triage``
          (druggability, surface class, signaling state, ``modality_hint`` with the
          rule behind it, the resolved structure tier, and risk ``flags``).
        * *What does this receptor imply about a binder that hit it?* →
          ``binder_context`` (internalization fate/kinetics, coupling and arrestins,
          rotamer hotspot residues, citation PMIDs).

        Non-signaling targets return ``available=False`` with ``known_target`` and
        ``receptordb_complexes`` instead of an error — they are still designable.

        >>> prof = client.proteins.receptor_profile("OPRM1")
        >>> ri = prof.receptor_intelligence
        >>> ri["target_triage"]["modality_hint"]
        'peptide_favourable'
        >>> [f["code"] for f in ri["target_triage"]["flags"]]
        ['fast_internalization']

        Complements :meth:`receptor_atlas` (raw curated overlay) and
        :meth:`receptor_intelligence` (pro+ endocytosis/biased-agonism profile).
        """
        return ReceptorProfile.model_validate(
            self._transport.request(
                "GET", f"/api/receptor-intelligence/profile/{gene}"
            ) or {"gene": gene}
        )

    def receptor_atlas(self, gene: str, full: bool = False) -> ReceptorAtlas:
        """Full deterministic Receptor Intelligence atlas overlay for ``gene``.

        This is the agent-callable surface for the complete Receptor
        Intelligence. It complements :meth:`receptor_intelligence` (the
        pro+ *profile* — endocytosis / internalization / biased agonism) by
        exposing the deterministic 21-atlas overlay and, for superadmin, the
        ndLF / nanoGPT supervised-head model predictions.

        Two tiers, one method:

        * ``full=False`` (default) — ``GET /api/receptor-intelligence/atlas/{gene}``.
          Hard-data overlay available to ANY authenticated user (free tier
          included). Returns whatever IS curated: ``identity``,
          ``signaling_state``, ``cell_surface``, ``coupling``, ``bias_profile``,
          ``engagements``, ``internalization``, ``rotamer_summary``,
          ``atlas_coverage_count``. Blocks with no curated data are silently
          omitted (no negative-absence messages).

        * ``full=True`` — ``GET /api/receptor-intelligence/atlas/{gene}/full``.
          SUPERADMIN ONLY. Adds the ndLF / nanoGPT ``model_predictions`` (22+
          supervised heads + atlas-vs-model ``comparison``), the full
          ``intracellular_partners`` list, ``trigger_profile``,
          ``phospho_bias_proxy``, ``residue_functional_annotation``,
          ``rotamer_full`` chi1 rows, ``tumbler_signature``, the
          ``disagreements`` panel, and raw ``atlas_coverage`` provenance.
          Authorization rides the client's api-key (the server enforces the
          superadmin gate); a non-superadmin key receives HTTP 403.

        The returned :class:`ReceptorAtlas` is permissive — every block is
        optional and additive server blocks are preserved as raw attributes.

        bd-LIGANDAI_ALPHA_V2-q3z1b. See :meth:`receptor_intelligence` for the
        narrower profile method.
        """
        suffix = "/full" if full else ""
        return ReceptorAtlas.model_validate(
            self._transport.request(
                "GET", f"/api/receptor-intelligence/atlas/{gene}{suffix}"
            )
            or {"gene": gene}
        )

    def variants(
        self,
        gene: str | None = None,
        include_shared: bool = True,
    ) -> list[ProteinVariant]:
        params: dict[str, object] = {"include_shared": include_shared}
        if gene is not None:
            params["gene"] = gene
        payload = self._transport.request("GET", "/api/protein-variants", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("variants", [])
        return [ProteinVariant.model_validate(v) for v in items]

    def get_variant(self, variant_id: int) -> ProteinVariant:
        return ProteinVariant.model_validate(
            self._transport.request("GET", f"/api/protein-variants/{variant_id}") or {"id": variant_id, "gene": ""}
        )

    def save_fold_as_variant(self, fold_job_id: str, gene: str, alias: str) -> ProteinVariant:
        return ProteinVariant.model_validate(
            self._transport.request(
                "POST",
                "/api/protein-variants/from-fold",
                json={"foldJobId": fold_job_id, "gene": gene, "alias": alias},
            )
            or {"id": 0, "gene": gene, "alias": alias}
        )

    def delete_variant(self, variant_id: int) -> bool:
        try:
            self._transport.request("DELETE", f"/api/protein-variants/{variant_id}")
            return True
        except LigandAILegalAcceptanceRequired:
            # ⛔ A legal 403 must not read as "server refused".
            raise
        except Exception as exc:
            # ⛔ `False` here must mean ONLY 'the server was reached and
            # declined'. Every other cause — no connection, bad key, CDN
            # block, 5xx — leaves the outcome UNKNOWN and now raises,
            # because a caller acting on a fabricated 'it did not happen'
            # is worse than one handling an error. [bd-dre-rp11j.3]
            resolve_mutation_failure(exc, operation="proteins.delete_variant", target=str(variant_id))
            return False

    def upload_pdb(
        self,
        file: Path | str,
        gene: str,
        custom_name: str | None = None,
    ) -> UserProtein:
        """Upload a custom PDB / CIF / mmCIF / ENT file to your private
        protein library and return the registered :class:`UserProtein`.

        Available to all authenticated tiers (free included).

        Pass the returned ``protein.id`` as ``user_pdb_id=`` to
        :meth:`Peptides.generate` to design against the uploaded structure::

            protein = client.proteins.upload_pdb("brd4_et.pdb", gene="BRD4")
            job = client.peptides.generate(
                gene="BRD4",              # routing / label only
                user_pdb_id=protein.id,   # <- YOUR coordinates
                num_peptides=200,
            )

        ``user_pdb_id`` is what emits ``userPdbId`` +
        ``pdbId="userpdb_<id>"`` + ``activeStructureSource="user_pdb"`` on the
        wire, and that triple is what makes the server load your file instead
        of the canonical structure for ``gene``.

        .. warning::
           Do **not** pass this id as ``variant_id=``. That argument resolves
           against the separate ``protein_variants`` store (populated by
           :meth:`Proteins.save_fold_as_variant`), which has an INDEPENDENT id
           sequence. An id from this method will either miss — silently falling
           back to the canonical gene structure — or collide with an unrelated
           variant row and design against the wrong protein entirely.
        """
        path = Path(file)
        ext = path.suffix.lower()
        mime = "chemical/x-mmcif" if ext in (".cif", ".mmcif") else "chemical/x-pdb"
        with path.open("rb") as f:
            # The platform expects the multipart field name "files" (plural).
            files = {"files": (path.name, f, mime)}
            data = {"gene": gene}
            if custom_name is not None:
                data["customName"] = custom_name
            payload = self._transport.request(
                "POST", "/api/user/proteins/upload", data=data, files=files
            ) or {}
        # Server returns {"created": N, "proteins": [UserProtein, ...], "errors": [...]}.
        # Unwrap the first protein when present; fall back to validating the raw
        # payload for older server builds that returned a flat object.
        if isinstance(payload, dict) and isinstance(payload.get("proteins"), list) and payload["proteins"]:
            return UserProtein.model_validate(payload["proteins"][0])
        return UserProtein.model_validate(payload)


class AsyncProteins(AsyncResource):
    async def info(
        self,
        query: str,
        include_sequence: bool = True,
        include_ptms: bool = True,
        include_domains: bool = True,
    ) -> ProteinInfo:
        params = {
            "include_sequence": include_sequence,
            "include_ptms": include_ptms,
            "include_domains": include_domains,
        }
        return ProteinInfo.model_validate(
            await self._transport.request("GET", f"/api/protein-info/{query}", params=params)
            or {"gene": query}
        )

    async def disorder_profile(self, gene: str) -> DisorderProfile:
        return DisorderProfile.model_validate(
            await self._transport.request("GET", f"/api/protein-info/disorder/{gene}") or {"gene": gene}
        )

    async def receptor_topology(self, gene: str) -> ReceptorTopology:
        return ReceptorTopology.model_validate(
            await self._transport.request("GET", f"/api/receptor-topology/{gene}") or {"gene": gene}
        )

    async def receptor_intelligence(
        self,
        gene: str | None = None,
        genes: list[str] | None = None,
    ) -> ReceptorIntelligence | dict[str, ReceptorIntelligence]:
        if genes is not None:
            payload = await self._transport.request(
                "POST", "/api/receptor-intelligence/batch", json={"genes": genes}
            ) or {}
            results = payload.get("results", payload)
            return {g: ReceptorIntelligence.model_validate(v) for g, v in results.items()}
        if gene is None:
            raise ValueError("Pass gene= or genes=")
        return ReceptorIntelligence.model_validate(
            await self._transport.request("GET", f"/api/receptor-intelligence/{gene}") or {"gene": gene}
        )

    async def receptor_profile(self, gene: str) -> ReceptorProfile:
        """Async variant of :meth:`Proteins.receptor_profile`.

        Decision-oriented receptor profile — ``target_triage`` before generation,
        ``binder_context`` after folding. Requires academia/pro/enterprise or higher.
        Non-signaling targets return ``available=False`` with ReceptorDB context
        rather than an error.
        """
        return ReceptorProfile.model_validate(
            await self._transport.request(
                "GET", f"/api/receptor-intelligence/profile/{gene}"
            ) or {"gene": gene}
        )

    async def receptor_atlas(self, gene: str, full: bool = False) -> ReceptorAtlas:
        """Async variant of :meth:`Proteins.receptor_atlas`.

        ``full=False`` → hard-data atlas overlay (any authenticated user).
        ``full=True`` → SUPERADMIN ndLF / nanoGPT model predictions overlay.
        bd-LIGANDAI_ALPHA_V2-q3z1b.
        """
        suffix = "/full" if full else ""
        return ReceptorAtlas.model_validate(
            await self._transport.request(
                "GET", f"/api/receptor-intelligence/atlas/{gene}{suffix}"
            )
            or {"gene": gene}
        )

    async def variants(
        self,
        gene: str | None = None,
        include_shared: bool = True,
    ) -> list[ProteinVariant]:
        params: dict[str, object] = {"include_shared": include_shared}
        if gene is not None:
            params["gene"] = gene
        payload = await self._transport.request("GET", "/api/protein-variants", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("variants", [])
        return [ProteinVariant.model_validate(v) for v in items]

    async def get_variant(self, variant_id: int) -> ProteinVariant:
        return ProteinVariant.model_validate(
            await self._transport.request("GET", f"/api/protein-variants/{variant_id}")
            or {"id": variant_id, "gene": ""}
        )

    async def save_fold_as_variant(self, fold_job_id: str, gene: str, alias: str) -> ProteinVariant:
        return ProteinVariant.model_validate(
            await self._transport.request(
                "POST",
                "/api/protein-variants/from-fold",
                json={"foldJobId": fold_job_id, "gene": gene, "alias": alias},
            )
            or {"id": 0, "gene": gene, "alias": alias}
        )

    async def delete_variant(self, variant_id: int) -> bool:
        try:
            await self._transport.request("DELETE", f"/api/protein-variants/{variant_id}")
            return True
        except LigandAILegalAcceptanceRequired:
            # ⛔ A legal 403 must not read as "server refused".
            raise
        except Exception as exc:
            # ⛔ `False` here must mean ONLY 'the server was reached and
            # declined'. Every other cause — no connection, bad key, CDN
            # block, 5xx — leaves the outcome UNKNOWN and now raises,
            # because a caller acting on a fabricated 'it did not happen'
            # is worse than one handling an error. [bd-dre-rp11j.3]
            resolve_mutation_failure(exc, operation="proteins.delete_variant", target=str(variant_id))
            return False

    async def upload_pdb(
        self,
        file: Path | str,
        gene: str,
        custom_name: str | None = None,
    ) -> UserProtein:
        """Async variant of :meth:`Proteins.upload_pdb`. Available to all
        authenticated tiers.
        """
        path = Path(file)
        ext = path.suffix.lower()
        mime = "chemical/x-mmcif" if ext in (".cif", ".mmcif") else "chemical/x-pdb"
        with path.open("rb") as f:
            files = {"files": (path.name, f, mime)}
            data = {"gene": gene}
            if custom_name is not None:
                data["customName"] = custom_name
            payload = await self._transport.request(
                "POST", "/api/user/proteins/upload", data=data, files=files
            ) or {}
        if isinstance(payload, dict) and isinstance(payload.get("proteins"), list) and payload["proteins"]:
            return UserProtein.model_validate(payload["proteins"][0])
        return UserProtein.model_validate(payload)
