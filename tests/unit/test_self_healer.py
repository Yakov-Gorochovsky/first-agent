"""
tests/unit/test_self_healer.py - Unit tests for the 4-Pillar ScorecardEngine, Tradeoff Evaluator, and SelfHealer.
"""

from core.self_healer import ScorecardEngine, SelfHealer


def test_scorecard_engine_evaluates_four_pillars_and_detects_violations() -> None:
    engine = ScorecardEngine()
    card = engine.evaluate(mission_id="m_default_bc29580e6f9a", reflect_code_fixes=False)

    assert card.mission_id == "m_default_bc29580e6f9a"
    assert 0.0 <= card.pillar_a_quality <= 100.0
    assert 0.0 <= card.pillar_b_token_economy <= 100.0
    assert 0.0 <= card.pillar_c_speed_autonomy <= 100.0
    assert 0.0 <= card.pillar_d_code_cleanliness <= 100.0
    assert 0.0 <= card.composite_score <= 100.0

    rule_ids = {v.rule_id for v in card.violations}
    assert "G-A1" in rule_ids
    assert "G-B1" in rule_ids
    assert "G-D1" in rule_ids


def test_tradeoff_evaluator_distinguishes_strict_win_and_worthwhile_investment() -> None:
    engine = ScorecardEngine()
    run2 = engine.evaluate(mission_id="m_default_610387c77508", reflect_code_fixes=False)
    run3 = engine.evaluate(mission_id="m_default_bc29580e6f9a", reflect_code_fixes=False)

    # Run 2 -> Run 3 spent more tokens (+22k) to activate 4/4 roles and probe 4 sub-assets (+16 Quality pts)
    tradeoff = engine.evaluate_tradeoff(run2, run3)
    assert tradeoff["should_keep"] is True
    assert tradeoff["verdict"] in ("STRICT_WIN", "WORTHWHILE_INVESTMENT")
    assert tradeoff["deltas"]["pillar_a_quality"] > 0


def test_self_healer_status_report_structure() -> None:
    healer = SelfHealer()
    status = healer.get_status_report()
    assert "current_scorecard" in status
    assert "baseline_before_healing" in status
    assert "historical_runs" in status
    assert len(status["historical_runs"]) == 3
    assert "guidelines" in status
    assert "Pillar A" in status["guidelines"]
