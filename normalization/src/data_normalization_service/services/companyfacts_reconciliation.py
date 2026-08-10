"""
SEC companyfacts reconciliation pass.

Fills genuine extraction gaps using the SEC companyfacts API. By default it runs
in *edge* mode: for every non-dimensional main-line concept the company reports,
any period missing from our store -- whether interior (interpolation) or at the
trailing/leading edge (e.g. the just-filed latest period) -- is offered to the
matcher. A value is inserted ONLY when companyfacts contains a fact whose period
end-date and duration bucket match, so the latest filing's gaps are recovered on
the same run yet nothing is ever fabricated. Pass include_edge=False (CLI:
--interpolation-only) to restrict to strictly-interior interpolation gaps.

These gaps arise when our presentation-linkbase-based extraction does not surface
a fact for a given filing (e.g. the concept appears only in a comparative column
of a later filing, or under a statement role we do not map). companyfacts exposes
the company's complete fact set regardless of presentation, so it can recover them.

This pass is INSERT-ONLY and never overwrites primary-extracted values. Every
value it inserts is tagged `source='sec_companyfacts'` for provenance. It only
fills NON-dimensional main-line concepts (companyfacts has no segment dimensions)
and matches the exact period end-date AND duration bucket (annual vs quarterly vs
instant) so it can never put a YTD figure into a quarterly slot.

For per-filing incremental use, pass target_period_end to reconcile_company so
only the freshly-processed filing's period is reconciled (still one API call).
"""
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from ..core.concept_canonicalization import canonical_concept, equivalence_class
from ..core.models import ValueDocument

logger = logging.getLogger(__name__)

# Duration windows (days) used to classify a companyfacts duration fact.
_ANNUAL_MIN, _ANNUAL_MAX = 340, 380
_QUARTERLY_MIN, _QUARTERLY_MAX = 80, 100


class CompanyFactsReconciliationService:
    """Recover genuine extraction gaps from the SEC companyfacts API."""

    def __init__(self, target_db, sec_client):
        """
        Args:
            target_db: pymongo Database handle for the normalize_data DB.
            sec_client: object exposing get_company_facts(cik) -> dict | None
                        (e.g. api.sec_client.SECFinancialClient).
        """
        self.db = target_db
        self.sec_client = sec_client

    # ------------------------------------------------------------------ #
    # Gap detection
    # ------------------------------------------------------------------ #
    def _find_interpolation_gaps(
        self, cik: str, concepts_coll: str, values_coll: str,
        include_edge: bool = True,
    ) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, str], ObjectId]]:
        """Return fillable gaps for a company and a map for concept lookup.

        A gap dict has: statement_type, canonical, period_end (datetime),
        period_str, is_instant (bool inferred from existing values).

        With ``include_edge=False`` only strict *interpolation* gaps are
        returned (a missing period that lies inside a concept group's own
        min..max filled range). With ``include_edge=True`` (default) the
        trailing/leading edges are included as well: any period present in the
        statement-type's universe that this concept group is missing. This lets
        a freshly-filed period (which is, by definition, the latest = the
        trailing edge) be reconciled immediately. It is safe because a value is
        only ever inserted when companyfacts actually confirms a matching fact
        exists -- nothing is fabricated.

        concept_index maps (statement_type, concept_name) -> concept_id so the
        recovered value can be attached to the right concept document.
        """
        concepts = list(self.db[concepts_coll].find({"cik": cik}))
        concept_index: Dict[Tuple[str, str], ObjectId] = {}
        # group_concepts[(stype, canonical)] = list of concept_ids
        group_concepts: Dict[Tuple[str, str], List[ObjectId]] = defaultdict(list)
        is_real: Dict[ObjectId, bool] = {}
        for c in concepts:
            cid = c["_id"]
            concept_index[(c["statement_type"], c["concept"])] = cid
            real = not c.get("abstract", False) and not c.get("dimension", False)
            is_real[cid] = real
            if real:
                canon = c.get("canonical_concept") or c["concept"]
                group_concepts[(c["statement_type"], canon)].append(cid)

        # Gather filled periods per group and per statement_type universe.
        all_cids = [cid for cids in group_concepts.values() for cid in cids]
        filled_periods: Dict[Tuple[str, str], set] = defaultdict(set)
        stype_universe: Dict[str, set] = defaultdict(set)
        period_is_instant: Dict[Tuple[str, datetime], bool] = {}
        # Per group, map a period's calendar month -> typical duration in days.
        # Quarterly cash-flow values are stored as YTD durations (3/6/9/12 months),
        # so the expected duration depends on the fiscal quarter (≈ its end month).
        group_month_duration: Dict[Tuple[str, str], Dict[int, int]] = defaultdict(dict)

        cid_to_group = {}
        for (stype, canon), cids in group_concepts.items():
            for cid in cids:
                cid_to_group[cid] = (stype, canon)

        for v in self.db[values_coll].find(
            {"concept_id": {"$in": all_cids}, "value": {"$ne": None}},
            {"concept_id": 1, "reporting_period": 1},
        ):
            grp = cid_to_group.get(v["concept_id"])
            if not grp:
                continue
            stype, canon = grp
            rp = v.get("reporting_period") or {}
            end = rp.get("end_date")
            if not isinstance(end, datetime):
                continue
            filled_periods[(stype, canon)].add(end)
            stype_universe[stype].add(end)
            # Infer instant vs duration from item_period ("start to end").
            ip = rp.get("item_period") or ""
            is_instant = " to " not in ip
            period_is_instant[(stype, end)] = is_instant
            if not is_instant:
                dur = self._item_period_days(ip)
                if dur is not None:
                    group_month_duration[(stype, canon)][end.month] = dur

        gaps: List[Dict[str, Any]] = []
        for (stype, canon), present in filled_periods.items():
            if not present:
                continue
            lo, hi = min(present), max(present)
            for p in stype_universe[stype]:
                if p in present:
                    continue
                # Interior periods are always candidates. Edge periods
                # (p <= lo or p >= hi) are candidates only in edge mode; they
                # stay fabrication-proof because _match_fact requires a
                # confirming companyfacts fact before anything is inserted.
                if not include_edge and not (lo < p < hi):
                    continue
                gaps.append({
                    "statement_type": stype,
                    "canonical": canon,
                    "period_end": p,
                    "period_str": p.strftime("%Y-%m-%d"),
                    "is_instant": period_is_instant.get((stype, p), False),
                    # Expected duration (days) learned from same-month filled
                    # values in this group; None if unknown.
                    "expected_duration": group_month_duration[(stype, canon)].get(p.month),
                })
        return gaps, concept_index

    @staticmethod
    def _item_period_days(item_period: str) -> Optional[int]:
        """Parse 'YYYY-MM-DD ... to YYYY-MM-DD ...' into a duration in days."""
        try:
            start_s, end_s = item_period.split(" to ")
            start = datetime.strptime(start_s.strip()[:10], "%Y-%m-%d")
            end = datetime.strptime(end_s.strip()[:10], "%Y-%m-%d")
            return (end - start).days
        except (ValueError, AttributeError):
            return None

    # ------------------------------------------------------------------ #
    # companyfacts lookup
    # ------------------------------------------------------------------ #
    @staticmethod
    def _duration_ok(start: str, end: str, frequency: str,
                     expected: Optional[int] = None) -> bool:
        try:
            d = (datetime.strptime(end, "%Y-%m-%d") -
                 datetime.strptime(start, "%Y-%m-%d")).days
        except (ValueError, TypeError):
            return False
        # Prefer the duration convention learned from the group's filled values
        # (handles YTD-cumulative quarterly cash-flow values: 3/6/9-month).
        if expected is not None:
            return abs(d - expected) <= 12
        if frequency == "annual":
            return _ANNUAL_MIN <= d <= _ANNUAL_MAX
        return _QUARTERLY_MIN <= d <= _QUARTERLY_MAX

    def _match_fact(
        self, facts: Dict[str, Any], tags: Tuple[str, ...], gap: Dict[str, Any],
        frequency: str,
    ) -> Optional[Dict[str, Any]]:
        """Find a companyfacts fact matching the gap's period and duration.

        Returns a dict with value, tag, unit, decimals, accession, start, end
        or None. Prefers the canonical tag and the matching form type.
        """
        want_form = "10-K" if frequency == "annual" else "10-Q"
        end_str = gap["period_str"]
        expected = gap.get("expected_duration")
        candidates = []
        usgaap = (facts.get("facts") or {}).get("us-gaap") or {}
        for tag in tags:
            short = tag.split(":", 1)[-1]
            concept_facts = usgaap.get(short)
            if not concept_facts:
                continue
            for unit, arr in (concept_facts.get("units") or {}).items():
                for f in arr:
                    if f.get("end") != end_str:
                        continue
                    start = f.get("start")
                    if gap["is_instant"]:
                        if start:  # gap is a stock; skip duration facts
                            continue
                    else:
                        if not start or not self._duration_ok(
                                start, end_str, frequency, expected):
                            continue
                    form = f.get("form", "")
                    candidates.append({
                        "value": f.get("val"),
                        "tag": tag,
                        "short_tag": short,
                        "unit": unit,
                        "decimals": f.get("decimals"),
                        "accession": f.get("accn"),
                        "start": start,
                        "end": end_str,
                        "frame": f.get("frame"),
                        "form_match": form.startswith(want_form),
                        "fy": f.get("fy"),
                    })
        if not candidates:
            return None
        # Prefer: form match, then a "frame" (SEC's canonical period), then
        # the canonical tag order (tags[0] is canonical).
        tag_rank = {t: i for i, t in enumerate(tags)}
        candidates.sort(key=lambda c: (
            not c["form_match"],
            c["frame"] is None,
            tag_rank.get(c["tag"], 99),
        ))
        return candidates[0]

    # ------------------------------------------------------------------ #
    # Orchestration
    # ------------------------------------------------------------------ #
    def reconcile_company(
        self, cik: str, frequency: str = "annual", dry_run: bool = False,
        include_edge: bool = True,
        target_period_end: Optional[datetime] = None,
    ) -> Dict[str, int]:
        """Fill genuine gaps for one company+frequency from companyfacts.

        ``include_edge=True`` (default) also fills trailing/leading-edge
        periods, so the latest filing's gaps are recovered on the same run.
        Set ``include_edge=False`` for interpolation-only behaviour.

        ``target_period_end`` restricts reconciliation to a single period (the
        end-date of a just-processed filing). Use this for per-filing
        incremental reconciliation: it still makes one companyfacts call but
        only attempts to fill that filing's missing concepts.

        Returns counts: gaps, filled, skipped_absent, skipped_exists.
        """
        if frequency == "annual":
            concepts_coll, values_coll = ("normalized_concepts_annual",
                                          "concept_values_annual")
            form_type = "10-K"
        else:
            concepts_coll, values_coll = ("normalized_concepts_quarterly",
                                          "concept_values_quarterly")
            form_type = "10-Q"

        # Retrieve fiscal_year_end_code so the reporting_period can include quarter.
        fiscal_year_end_code: Optional[str] = None
        try:
            company_doc = self.db.companies.find_one(
                {"cik": str(cik)}, {"corporate_info.fiscal_year_end": 1}
            )
            if company_doc:
                fiscal_year_end_code = (
                    company_doc.get("corporate_info", {}).get("fiscal_year_end")
                )
        except Exception:
            pass

        gaps, concept_index = self._find_interpolation_gaps(
            cik, concepts_coll, values_coll, include_edge=include_edge)
        if target_period_end is not None:
            gaps = [g for g in gaps if g["period_end"] == target_period_end]

        # Stamp fiscal_year_end_code on every gap so _build_reporting_period can compute quarter.
        if fiscal_year_end_code:
            for g in gaps:
                g["fiscal_year_end_code"] = fiscal_year_end_code
        stats = {"gaps": len(gaps), "filled": 0,
                 "skipped_absent": 0, "skipped_exists": 0}
        if not gaps:
            return stats

        facts = self.sec_client.get_company_facts(cik)
        if not facts:
            logger.warning(f"No companyfacts for CIK {cik}; cannot reconcile")
            stats["skipped_absent"] = len(gaps)
            return stats

        vcoll = self.db[values_coll]
        for gap in gaps:
            tags = equivalence_class(gap["canonical"])
            match = self._match_fact(facts, tags, gap, frequency)
            if not match or match["value"] is None:
                stats["skipped_absent"] += 1
                continue

            # Attach to the concept doc for the exact tag companyfacts used,
            # else fall back to the canonical representative we have.
            concept_id = concept_index.get((gap["statement_type"], match["tag"]))
            if concept_id is None:
                concept_id = concept_index.get(
                    (gap["statement_type"], gap["canonical"]))
            if concept_id is None:
                # Find any concept in the group we actually store.
                for t in tags:
                    concept_id = concept_index.get((gap["statement_type"], t))
                    if concept_id is not None:
                        break
            if concept_id is None:
                stats["skipped_absent"] += 1
                continue

            reporting_period = self._build_reporting_period(
                cik, gap, match, form_type)

            # FIX: was checking reporting_period.end_date (a datetime) which misses
            # matches whenever there's a timezone offset between the existing stored
            # value and the freshly-built gap-fill value. Check on the unique key
            # {concept_id, fiscal_year, quarter} instead — same fields as the DB's
            # unique index — so this pre-check and the index agree.
            exists_filter = {
                "concept_id": concept_id,
                "cik": cik,
                "reporting_period.fiscal_year": reporting_period.get("fiscal_year"),
                "dimension_value": False,
                "calculated": False,  # reconciliation always inserts calculated=False
            }
            if reporting_period.get("quarter") is not None:
                exists_filter["reporting_period.quarter"] = reporting_period["quarter"]
            if vcoll.find_one(exists_filter):
                stats["skipped_exists"] += 1
                continue

            if dry_run:
                stats["filled"] += 1
                logger.info(
                    f"[DRY] would fill {match['short_tag']} "
                    f"{gap['period_str']} = {match['value']} (cik {cik})")
                continue

            value_doc = ValueDocument(
                concept_id=concept_id,
                company_cik=cik,
                statement_type=gap["statement_type"],
                form_type=form_type,
                reporting_period=reporting_period,
                value=float(match["value"]) if isinstance(match["value"], (int, float))
                else match["value"],
                created_at=datetime.now(),
                dimension_value=False,
                decimals=str(match["decimals"]) if match["decimals"] is not None else None,
                source="sec_companyfacts",
            )
            doc = value_doc.to_dict()
            doc["calculated"] = False
            doc.pop("_id", None)

            # FIX: atomic insert + DuplicateKeyError catch as the final safety net
            # (the pre-check above closes the common case, the unique index closes
            # the race-condition case).
            try:
                vcoll.insert_one(doc)
                stats["filled"] += 1
            except DuplicateKeyError:
                stats["skipped_exists"] += 1

        return stats

    @staticmethod
    def _build_reporting_period(
        cik: str, gap: Dict[str, Any], match: Dict[str, Any], form_type: str
    ) -> Dict[str, Any]:
        end_dt = gap["period_end"]
        fiscal_year = match.get("fy") or end_dt.year
        quarter = None
        # Derive fiscal_year and quarter from the VALUE's own period end date.
        # The SEC companyfacts API tags comparative values (prior-year column) with
        # the FILING's fiscal year (e.g. fy=2026 for a balance-sheet date of
        # 2025-03-02 that appeared in LEVI's Q1 FY2026 10-Q).  Using the API's fy
        # for comparatives is wrong; we must recompute from the period end date so
        # the comparative correctly lands in the prior fiscal year.
        fye = gap.get("fiscal_year_end_code") or ""
        if fye:
            try:
                from utilities.helpers.period_utils import FiscalYearCalculator
                computed_fy, computed_q = FiscalYearCalculator.calculate_fiscal_year_and_quarter(
                    end_dt, fye
                )
                if computed_fy:
                    fiscal_year = computed_fy   # override API fy
                if computed_q is not None:
                    quarter = computed_q
            except Exception:
                pass  # non-fatal; fall back to API fy and no quarter
        # Build canonical reporting_period with only essential fields
        # accession_number intentionally omitted: gap-fill rows are sourced
        # from the SEC companyfacts API and do not belong to any single filing.
        rp: Dict[str, Any] = {
            "end_date": end_dt,
            "period_date": gap["period_str"],
            "fiscal_year": fiscal_year,
        }
        if quarter is not None and form_type == "10-Q":
            rp["quarter"] = quarter
        return rp
