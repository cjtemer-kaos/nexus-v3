"""Tests del DecisionEngine (estaba sin cobertura y con un import roto a command_protocol)."""
from src.core.decision_engine import DecisionEngine


def test_module_imports_even_without_legacy_command_protocol():
    # Si src.core.command_protocol no existe (bundle portable), el motor debe seguir importable.
    d = DecisionEngine()
    cmds = d.decompose("arregla un bug en el parser")
    assert cmds, "no descompuso nada"
    assert all(c.target and c.action for c in cmds)
    assert all(isinstance(c.instruction, dict) for c in cmds)


def test_assign_picks_a_target():
    d = DecisionEngine()
    assert d.assign("implementa un endpoint HTTP")


def test_evaluate_detects_failure():
    d = DecisionEngine()
    ok = d.evaluate(output="listo", exit_code=0)
    bad = d.evaluate(output="", exit_code=1, error="boom")
    assert ok != bad
