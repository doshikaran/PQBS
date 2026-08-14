"""Unit tests for A15 RedTeamAgent, CorpusLoader, and MetricsComputer.

All tests use real corpus files or in-memory data. No live DB required.
Gate and DB connections are mocked where needed.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from pqbs.agents.integrity.a15_redteam import (
    CorpusLoader,
    EntryResult,
    EvalReport,
    MetricsComputer,
    RedTeamAgent,
    _DEMO_TENANT_ID,
    _EVAL_TENANT_ID,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CORPUS_DIR = Path(__file__).parents[2] / "eval" / "corpus"


def _make_entry_result(
    label: str,
    threat_class: str | None,
    expected_verdict: str,
    actual_verdict: str,
    signal_scores: dict[str, float] | None = None,
    evasion_target: str | None = None,
    screening_latency_ms: float = 10.0,
) -> EntryResult:
    return EntryResult(
        corpus_id=f"test_{label}_{uuid4().hex[:6]}",
        label=label,
        threat_class=threat_class,
        expected_verdict=expected_verdict,
        actual_verdict=actual_verdict,
        correct=(actual_verdict == expected_verdict),
        screening_latency_ms=screening_latency_ms,
        signal_scores=signal_scores or {},
        evasion_target=evasion_target,
    )


# ---------------------------------------------------------------------------
# 1. CorpusLoader loads all three corpus files with correct counts
# ---------------------------------------------------------------------------


def test_corpus_loader_loads_all_three_sets():
    """CorpusLoader returns exactly 200/100/50 entries from real corpus files."""
    loader = CorpusLoader(corpus_dir=_CORPUS_DIR)
    benign = loader.load_benign()
    poison = loader.load_poison()
    evasion = loader.load_evasion()

    assert len(benign) == 200, f"Expected 200 benign entries, got {len(benign)}"
    assert len(poison) == 100, f"Expected 100 poison entries, got {len(poison)}"
    assert len(evasion) == 50, f"Expected 50 evasion entries, got {len(evasion)}"

    all_entries = loader.load_all()
    assert len(all_entries) == 350, f"Expected 350 total entries, got {len(all_entries)}"

    # Verify labels
    assert all(e.label == "benign" for e in benign)
    assert all(e.label == "poison" for e in poison)
    assert all(e.label == "evasion" for e in evasion)


# ---------------------------------------------------------------------------
# 2. CorpusLoader rejects entries missing required fields
# ---------------------------------------------------------------------------


def test_corpus_loader_rejects_missing_label_field(tmp_path: Path):
    """CorpusLoader raises ValueError when an entry is missing the 'label' field."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(
        '[{"corpus_id": "x001", '
        '"threat_class": null, "expected_verdict": "trusted", '
        '"subject": "C001", "predicate": "customer_tier", "object": "gold", '
        '"object_normalized": "gold", "source_type": "system_of_record", '
        '"source_uri": "crm://test", "source_trust_tier": "authoritative", '
        '"author_agent_id": "a1_ingestion", "confidence": 0.9, '
        '"evasion_target": null, "notes": "test"}]'
    )
    loader = CorpusLoader(corpus_dir=tmp_path)
    # Rename to a corpus filename
    (tmp_path / "benign.json").write_text(bad_file.read_text())

    with pytest.raises(ValueError, match="missing required fields"):
        loader.load_benign()


# ---------------------------------------------------------------------------
# 3. CorpusLoader rejects invalid label values
# ---------------------------------------------------------------------------


def test_corpus_loader_rejects_invalid_label(tmp_path: Path):
    """CorpusLoader raises ValueError when label='unknown'."""
    bad_entry = {
        "corpus_id": "x001",
        "label": "unknown",
        "threat_class": None,
        "expected_verdict": "trusted",
        "subject": "C001",
        "predicate": "customer_tier",
        "object": "gold",
        "object_normalized": "gold",
        "source_type": "system_of_record",
        "source_uri": "crm://test",
        "source_trust_tier": "authoritative",
        "author_agent_id": "a1_ingestion",
        "confidence": 0.9,
        "evasion_target": None,
        "notes": "test",
    }
    import json
    bad_file = tmp_path / "benign.json"
    bad_file.write_text(json.dumps([bad_entry]))

    loader = CorpusLoader(corpus_dir=tmp_path)
    with pytest.raises(ValueError, match="invalid label"):
        loader.load_benign()


# ---------------------------------------------------------------------------
# 4. MetricsComputer computes detection rate correctly
# ---------------------------------------------------------------------------


def test_metrics_computer_detection_rate():
    """3 of 5 poison entries quarantined → detection_rate_overall = 0.6."""
    results = [
        _make_entry_result("poison", "T1", "quarantined", "quarantined"),
        _make_entry_result("poison", "T1", "quarantined", "quarantined"),
        _make_entry_result("poison", "T1", "quarantined", "quarantined"),
        _make_entry_result("poison", "T1", "quarantined", "trusted"),  # missed
        _make_entry_result("poison", "T1", "quarantined", "inconclusive"),  # missed
    ]
    mc = MetricsComputer()
    metrics = mc.compute(results)
    assert abs(metrics.detection_rate_overall - 0.6) < 1e-6


# ---------------------------------------------------------------------------
# 5. MetricsComputer computes false positive rate correctly
# ---------------------------------------------------------------------------


def test_metrics_computer_false_positive_rate():
    """1 of 10 benign entries quarantined → false_positive_rate = 0.1."""
    results = [
        _make_entry_result("benign", None, "trusted", "quarantined"),  # FP
        *[
            _make_entry_result("benign", None, "trusted", "trusted")
            for _ in range(9)
        ],
    ]
    mc = MetricsComputer()
    metrics = mc.compute(results)
    assert abs(metrics.false_positive_rate - 0.1) < 1e-6


# ---------------------------------------------------------------------------
# 6. MetricsComputer computes evasion resistance correctly
# ---------------------------------------------------------------------------


def test_metrics_computer_evasion_resistance():
    """1 of 5 evasion entries quarantined → evasion_resistance = 0.2."""
    results = [
        _make_entry_result("evasion", None, "quarantined", "quarantined", evasion_target="S1"),
        *[
            _make_entry_result("evasion", None, "quarantined", "trusted", evasion_target="S1")
            for _ in range(4)
        ],
    ]
    mc = MetricsComputer()
    metrics = mc.compute(results)
    assert abs(metrics.evasion_resistance - 0.2) < 1e-6


# ---------------------------------------------------------------------------
# 7. MetricsComputer computes detection rate by class correctly
# ---------------------------------------------------------------------------


def test_metrics_computer_by_class():
    """T1: 2/4=0.5, T2: 2/2=1.0."""
    results = [
        _make_entry_result("poison", "T1", "quarantined", "quarantined"),
        _make_entry_result("poison", "T1", "quarantined", "quarantined"),
        _make_entry_result("poison", "T1", "quarantined", "trusted"),
        _make_entry_result("poison", "T1", "quarantined", "trusted"),
        _make_entry_result("poison", "T2", "quarantined", "quarantined"),
        _make_entry_result("poison", "T2", "quarantined", "quarantined"),
    ]
    mc = MetricsComputer()
    metrics = mc.compute(results)
    assert abs(metrics.detection_rate_by_class["T1"] - 0.5) < 1e-6
    assert abs(metrics.detection_rate_by_class["T2"] - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# 8. EvalReport serializes to a JSON-serializable dict
# ---------------------------------------------------------------------------


def test_eval_report_serializes_to_dict():
    """to_dict() returns a structure that is JSON-serializable."""
    import json as _json

    results = [
        _make_entry_result("benign", None, "trusted", "trusted"),
        _make_entry_result("poison", "T1", "quarantined", "quarantined"),
    ]
    mc = MetricsComputer()
    metrics = mc.compute(results)
    report = EvalReport(metrics=metrics, results=results)

    d = report.to_dict()
    # Must not raise
    serialized = _json.dumps(d)
    assert isinstance(serialized, str)
    assert "detection_rate_overall" in d["metrics"]
    assert len(d["results"]) == 2


# ---------------------------------------------------------------------------
# 9. A15 rejects eval_tenant_id == demo tenant UUID
# ---------------------------------------------------------------------------


def test_a15_rejects_demo_tenant():
    """RedTeamAgent raises ValueError if eval_tenant_id equals the demo tenant UUID."""
    with pytest.raises(ValueError, match="must not equal demo tenant"):
        RedTeamAgent(eval_tenant_id=_DEMO_TENANT_ID)


# ---------------------------------------------------------------------------
# 10. A15 has no delete, revoke, or alter methods
# ---------------------------------------------------------------------------


def test_a15_cannot_delete_or_revoke():
    """RedTeamAgent must not expose delete, revoke, or alter methods."""
    agent = RedTeamAgent(eval_tenant_id=_EVAL_TENANT_ID)
    forbidden = ["delete", "revoke", "alter"]
    for name in forbidden:
        assert not hasattr(agent, name), (
            f"RedTeamAgent must not have method '{name}' (Security Invariant 7)"
        )


# ---------------------------------------------------------------------------
# 11. Signal marginal contributions ablation calculation
# ---------------------------------------------------------------------------


def test_signal_marginal_contributions():
    """
    Verify ablation: for a 2-entry poison corpus where zeroing S3 would flip
    one entry from quarantined to trusted, the marginal contribution of S3 = 0.5.

    Signal weights (from gate.py):
      S3_imperative_content: 0.25
      S1_embedding_anomaly: 0.20
      S2_source_trust_tier: 0.20
      ...

    We construct two poison entries:
    - Entry A: trust_score = 0.80 (quarantined). With S3 ablated (S3→0.5):
        new_trust = 0.80 - 0.25 * (S3_score - 0.5)
        If S3_score = 1.0: new_trust = 0.80 - 0.25*0.5 = 0.675 → still quarantined (>=0.7)
      Let's use S3_score=1.0, trust_score=0.80.
      Ablated: 0.80 - 0.25*(1.0 - 0.5) = 0.675 — still quarantined.

    We need one that FLIPS. Let's use trust_score=0.71, S3_score=1.0:
      Ablated: 0.71 - 0.25*0.5 = 0.585 → inconclusive (not quarantined) ✓

    Actually the ablation formula is:
      new_trust = sum(w * s_new for all signals)
    where s_new = 0.5 for ablated signal, original otherwise.
    The difference = w_S3 * (s_S3 - 0.5)

    So for the flip case we need:
      original >= 0.7 but ablated < 0.7
      => w_S3 * (s_S3 - 0.5) >= 0.71 - 0.7 = 0.01 ... (trivially satisfied with S3=1.0, w=0.25)

    Let's construct a minimal example:
    - All signals 0.5 except S3=1.0 → trust = 0.5 + 0.25*(1.0-0.5) = 0.5+0.125 = 0.625 (inconclusive)

    Hmm, we need original >= 0.7. Let's use S2=1.0 and S3=1.0:
      trust = 0.5 + 0.20*(1.0-0.5) + 0.25*(1.0-0.5) = 0.5 + 0.10 + 0.125 = 0.725 ✓
      Ablate S3: trust = 0.5 + 0.20*(1.0-0.5) + 0.25*(0.5-0.5) = 0.5 + 0.10 = 0.60 → trusted ✓

    So: entry with S2=1.0, S3=1.0, all others 0.5 → quarantined, ablate S3 → trusted.
    Entry with S2=1.0, S3=1.0 same → both flip. That gives marginal = 2/2 - 0/2 = 1.0.

    Let's use one entry that flips (S3=1.0, S2=1.0) and one that doesn't (S3=0.5, S2=1.0):
    - Entry 1: all 0.5 except S2=1.0, S3=1.0 → trust=0.725 (quarantined)
               Ablate S3 → trust=0.60 (trusted): FLIPS
    - Entry 2: all 0.5 except S2=1.0, S3=0.5 → trust=0.60 (trusted): NOT quarantined

    That means only 1 of 2 is quarantined overall. Detection = 0.5.
    Ablated: 0 quarantined → rate = 0.0.
    Marginal of S3 = 0.5 - 0.0 = 0.5. ✓
    """
    # Build signal_scores dict for a full 8-signal set
    # Keys are in the form "s1_embedding_anomaly", etc.
    def _all_neutral_except(**overrides: float) -> dict[str, float]:
        base = {
            "s1_embedding_anomaly": 0.5,
            "s2_source_trust_tier": 0.5,
            "s3_imperative_content": 0.5,
            "s4_author_behavior": 0.5,
            "s5_contradiction_burst": 0.5,
            "s6_corroboration_diversity": 0.5,
            "s7_derivation_integrity": 0.5,
            "s8_temporal_plausibility": 0.5,
        }
        base.update(overrides)
        return base

    # Entry 1: S2=1.0, S3=1.0 → trust=0.725 (quarantined), ablate S3 → 0.60 (trusted)
    entry1 = _make_entry_result(
        label="poison",
        threat_class="T1",
        expected_verdict="quarantined",
        actual_verdict="quarantined",  # detected
        signal_scores=_all_neutral_except(
            s2_source_trust_tier=1.0,
            s3_imperative_content=1.0,
        ),
    )

    # Entry 2: S2=1.0, S3=0.5 → trust=0.60 (trusted) — not caught
    entry2 = _make_entry_result(
        label="poison",
        threat_class="T1",
        expected_verdict="quarantined",
        actual_verdict="trusted",  # not detected
        signal_scores=_all_neutral_except(
            s2_source_trust_tier=1.0,
            s3_imperative_content=0.5,
        ),
    )

    mc = MetricsComputer()
    metrics = mc.compute([entry1, entry2])

    # Overall detection rate = 1/2 = 0.5 (only entry1 is quarantined)
    assert abs(metrics.detection_rate_overall - 0.5) < 1e-6, (
        f"Expected detection_rate_overall=0.5, got {metrics.detection_rate_overall}"
    )

    # After ablating S3:
    #   entry1: trust goes from 0.725 to 0.60 → trusted (not quarantined)
    #   entry2: trust unchanged (S3 was already 0.5) → trusted (not quarantined)
    #   ablated quarantined = 0 → rate = 0.0
    # Marginal = 0.5 - 0.0 = 0.5
    s3_marginal = metrics.signal_marginal_contributions.get("s3_imperative_content")
    assert s3_marginal is not None, "s3_imperative_content missing from marginal contributions"
    assert abs(s3_marginal - 0.5) < 1e-4, (
        f"Expected S3 marginal contribution=0.5, got {s3_marginal}"
    )
