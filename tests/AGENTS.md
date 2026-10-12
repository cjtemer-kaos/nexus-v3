# AGENTS.md — tests

pytest. La suite debe terminar en **exit 0** antes de commitear.

- `tests/test_e2e_live.py`: E2E in-process contra el server (TestClient). Ahí van los tests
  de endpoints y de features nuevas (checkpoints, diagnostics, board, routers, sub-agentes…).
- Tests existentes que codifican comportamiento INTENCIONAL (no los "arregles" cambiando
  el código): p.ej. `test_session.py::test_delete_parent_with_children_fk_guard` espera que
  borrar un padre con hijos LANCE (guard FK). Si tu cambio lo rompe, ajustá tu cambio.
- Correr: `.venv-ft314\Scripts\python.exe -m pytest -q`
- Para endpoints nuevos, agregá un test que los ejercite (status 200 + forma de respuesta).
