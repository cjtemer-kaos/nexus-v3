# EXPORT_GAP — SuperNEXUS v2 → Nexus v3

> Generado 2026-09-06 — Task 7.1. Fuente: `D:\ias\proyectos\supernexus-v2` vs `D:\ias\proyectos
exus-v3`. Verbatim port, no reinventar.

**Resumen:** SN core 229 files (73 605 LOC) vs N3 core 18 files (3 779 LOC) → **216 faltantes core** + **8 tools** + **13 bridges** + subdirs = **239 entries**. P0→Fases 1-3/seguridad, P1→memoria/orquestacion, P2→posponer.

| Prioridad | Cant | Criterio |
|---|---|---|
| **P0 port verbatim** | 48 | Seguridad, MCP 38 tools, WorkspaceTools, a2a/event_bus, circuit_breaker/loop_guard |
| **P1 adaptar** | 43 | Memoria jerarquica/FTS5, director/conductor/DAG, RAG/smart_indexer, deep_research |
| **P2 posponer/descartar** | 148 | UI especificos, training pesado, experimentales |

**Orden topologico de port:** `auth_vault`/`credential_manager` → `ai_defence` → `capability_*` → `tool_guardrails`/`sandbox_service` → `mcp_*` + `bridges/mcp_bridge_server` → `tools/builtin`/`persistent_shell` → `checkpoint`/`checkpoint_contract` → `context_compactor`/`fts5_search` → `hierarchical_memory` → `director` slices (`conductor`→`dag_coordinator`→`background_workers`) → `event_bus`/`a2a_server`.

**Como portar:** copiar verbatim, adaptar solo imports relativos + `NEXUS_ROOT` env, registrar en `tool_registry.py` con `Scope`, exponer en `server.py` si tiene endpoint. Cada batch con test que falla antes.

| # | File | LOC | Prio | Origen | Nota parity |
|---|---|---|---|---|---|
| 1 | `bridges/mcp_bridge_server.py` | 3181 | **P0** | bridges | 38 tools MCP |
| 2 | `ai_tools.py` | 2886 | **P0** | core | 2 886 LOC tools |
| 3 | `ai_defence.py` | 935 | **P0** | core | 935 LOC seguridad |
| 4 | `peer_chat.py` | 874 | **P0** | core |  |
| 5 | `capability_discovery.py` | 664 | **P0** | core |  |
| 6 | `mcp_client_bridge.py` | 653 | **P0** | core |  |
| 7 | `hermes_tools.py` | 617 | **P0** | core |  |
| 8 | `checkpoint.py` | 409 | **P0** | core | Checkpoint Contract |
| 9 | `message_bus.py` | 376 | **P0** | core |  |
| 10 | `capability_security.py` | 371 | **P0** | core |  |
| 11 | `credential_manager.py` | 364 | **P0** | core |  |
| 12 | `tools/codegraph_tool.py` | 346 | **P0** | tools |  |
| 13 | `loop_detector.py` | 343 | **P0** | core |  |
| 14 | `doctor.py` | 311 | **P0** | core |  |
| 15 | `tools/builtin.py` | 291 | **P0** | tools | WorkspaceTools drive-root fallback |
| 16 | `bridges/hive_runner.py` | 288 | **P0** | bridges |  |
| 17 | `bridges/network_architecture.py` | 278 | **P0** | bridges |  |
| 18 | `event_bus.py` | 268 | **P0** | core | A2A / PC1-PC2 |
| 19 | `mcp_hub.py` | 264 | **P0** | core |  |
| 20 | `loop_guard.py` | 247 | **P0** | core |  |
| 21 | `tool_guardrails.py` | 244 | **P0** | core |  |
| 22 | `graceful_degradation.py` | 231 | **P0** | core |  |
| 23 | `prompt_security.py` | 231 | **P0** | core |  |
| 24 | `tools/persistent_shell.py` | 222 | **P0** | tools |  |
| 25 | `checkpoint_contract.py` | 215 | **P0** | core | Checkpoint Contract |
| 26 | `tools/web_fetch.py` | 209 | **P0** | tools |  |
| 27 | `bridges/redis_pubsub.py` | 196 | **P0** | bridges |  |
| 28 | `bridges/mcp_chat.py` | 183 | **P0** | bridges |  |
| 29 | `security_utils.py` | 178 | **P0** | core |  |
| 30 | `tools/code_search.py` | 178 | **P0** | tools |  |
| 31 | `bridges/agent_browser_mcp.py` | 177 | **P0** | bridges |  |
| 32 | `bridges/api_chat.py` | 177 | **P0** | bridges |  |
| 33 | `mcp_connection_manager.py` | 169 | **P0** | core |  |
| 34 | `approval_gate.py` | 156 | **P0** | core |  |
| 35 | `bridges/tailscale_bridge.py` | 153 | **P0** | bridges |  |
| 36 | `circuit_breaker.py` | 145 | **P0** | core |  |
| 37 | `mcp_registry_client.py` | 144 | **P0** | core |  |
| 38 | `bridges/nexus_bridge.py` | 133 | **P0** | bridges |  |
| 39 | `bridges/rest_adapter.py` | 129 | **P0** | bridges |  |
| 40 | `a2a_server.py` | 126 | **P0** | core | A2A / PC1-PC2 |
| 41 | `acp_protocol.py` | 124 | **P0** | core |  |
| 42 | `sandbox_service.py` | 121 | **P0** | core |  |
| 43 | `bridges/remote_node_bridge.py` | 118 | **P0** | bridges |  |
| 44 | `bridges/ssh_bridge.py` | 99 | **P0** | bridges |  |
| 45 | `auth_vault.py` | 96 | **P0** | core |  |
| 46 | `tools/skill_gateway.py` | 96 | **P0** | tools |  |
| 47 | `mcp_tools_cache.py` | 89 | **P0** | core |  |
| 48 | `bridges/mcp_autodiscovery.py` | 62 | **P0** | bridges |  |
| 49 | `director.py` | 1830 | **P1** | core | 1 830 LOC orchestrator |
| 50 | `hive_workflow_engine.py` | 1451 | **P1** | core |  |
| 51 | `background_workers.py` | 1211 | **P1** | core |  |
| 52 | `memory_consolidator.py` | 1194 | **P1** | core | 1 194 LOC memoria |
| 53 | `agent_teams.py` | 964 | **P1** | core |  |
| 54 | `smart_codebase_indexer.py` | 762 | **P1** | core |  |
| 55 | `recursive_seed_ai.py` | 724 | **P1** | core |  |
| 56 | `context_compactor.py` | 679 | **P1** | core | 679 LOC compaction L1-L4 |
| 57 | `learning_graph.py` | 675 | **P1** | core |  |
| 58 | `gema_host.py` | 628 | **P1** | core |  |
| 59 | `deep_research.py` | 590 | **P1** | core |  |
| 60 | `self_improving.py` | 575 | **P1** | core |  |
| 61 | `prompt_compressor.py` | 547 | **P1** | core |  |
| 62 | `memory_extractor.py` | 544 | **P1** | core |  |
| 63 | `task_queue.py` | 517 | **P1** | core |  |
| 64 | `dream_distill.py` | 512 | **P1** | core |  |
| 65 | `tools/cdp_browser.py` | 499 | **P1** | tools |  |
| 66 | `realtime_hub.py` | 483 | **P1** | core |  |
| 67 | `hybrid_memory.py` | 438 | **P1** | core |  |
| 68 | `task_executor.py` | 437 | **P1** | core |  |
| 69 | `dag_coordinator.py` | 429 | **P1** | core |  |
| 70 | `agent_runner.py` | 413 | **P1** | core |  |
| 71 | `orchestrator.py` | 378 | **P1** | core |  |
| 72 | `model_autopsy.py` | 376 | **P1** | core |  |
| 73 | `hermes_director.py` | 343 | **P1** | core | 1 830 LOC orchestrator |
| 74 | `scheduler.py` | 328 | **P1** | core |  |
| 75 | `conductor.py` | 321 | **P1** | core |  |
| 76 | `sub_agent_spawner.py` | 309 | **P1** | core |  |
| 77 | `rag_engine.py` | 303 | **P1** | core |  |
| 78 | `fts5_search.py` | 295 | **P1** | core | FTS5 default sin ChromaDB |
| 79 | `gema_worker.py` | 267 | **P1** | core |  |
| 80 | `tools/vision_gem_v2.py` | 255 | **P1** | tools |  |
| 81 | `hierarchical_memory.py` | 237 | **P1** | core |  |
| 82 | `knowledge_vault.py` | 236 | **P1** | core |  |
| 83 | `self_learning_loop.py` | 210 | **P1** | core |  |
| 84 | `swarm_lifecycle.py` | 206 | **P1** | core |  |
| 85 | `sub_director.py` | 191 | **P1** | core | 1 830 LOC orchestrator |
| 86 | `learning_loop.py` | 176 | **P1** | core |  |
| 87 | `task_lifecycle.py` | 167 | **P1** | core |  |
| 88 | `swarm_foundation.py` | 164 | **P1** | core |  |
| 89 | `dream_consolidation.py` | 148 | **P1** | core |  |
| 90 | `scoped_event_bus.py` | 107 | **P1** | core | A2A / PC1-PC2 |
| 91 | `skill_aware_director.py` | 39 | **P1** | core | 1 830 LOC orchestrator |
| 92 | `actor_base.py` | 832 | **P2** | core |  |
| 93 | `judge_pipeline.py` | 769 | **P2** | core |  |
| 94 | `curator.py` | 724 | **P2** | core |  |
| 95 | `llm_provider.py` | 658 | **P2** | core |  |
| 96 | `autopilot_service.py` | 641 | **P2** | core |  |
| 97 | `nexus_trainer.py` | 614 | **P2** | core |  |
| 98 | `self_model.py` | 604 | **P2** | core |  |
| 99 | `session_manager.py` | 603 | **P2** | core |  |
| 100 | `gallery.py` | 581 | **P2** | core |  |
| 101 | `data_collector.py` | 547 | **P2** | core |  |
| 102 | `fusion_engine.py` | 530 | **P2** | core |  |
| 103 | `multi_file_editor.py` | 529 | **P2** | core |  |
| 104 | `model_registry.py` | 507 | **P2** | core |  |
| 105 | `web_researcher.py` | 502 | **P2** | core |  |
| 106 | `loops_engine.py` | 498 | **P2** | core |  |
| 107 | `skill_lifecycle.py` | 488 | **P2** | core |  |
| 108 | `auto_skill_creator.py` | 481 | **P2** | core |  |
| 109 | `ollama.py` | 471 | **P2** | core |  |
| 110 | `provider_unified.py` | 471 | **P2** | core |  |
| 111 | `cookbook.py` | 449 | **P2** | core |  |
| 112 | `typed_events.py` | 449 | **P2** | core |  |
| 113 | `compose_workflow.py` | 439 | **P2** | core |  |
| 114 | `llm_gateway.py` | 417 | **P2** | core |  |
| 115 | `nexus_hive.py` | 409 | **P2** | core |  |
| 116 | `three_loop.py` | 408 | **P2** | core |  |
| 117 | `skill_extractor.py` | 405 | **P2** | core |  |
| 118 | `project_planner.py` | 400 | **P2** | core |  |
| 119 | `dev_loop.py` | 396 | **P2** | core |  |
| 120 | `mixture_of_agents.py` | 376 | **P2** | core |  |
| 121 | `ollama_manager.py` | 376 | **P2** | core |  |
| 122 | `recipe_engine.py` | 371 | **P2** | core |  |
| 123 | `resilience.py` | 362 | **P2** | core |  |
| 124 | `session_context_recovery.py` | 354 | **P2** | core |  |
| 125 | `gema_profiles.py` | 353 | **P2** | core |  |
| 126 | `cursor_checkpoint.py` | 342 | **P2** | core | Checkpoint Contract |
| 127 | `codebase_context.py` | 337 | **P2** | core |  |
| 128 | `self_improvement.py` | 333 | **P2** | core |  |
| 129 | `provider_catalog.py` | 331 | **P2** | core |  |
| 130 | `system_manager.py` | 328 | **P2** | core |  |
| 131 | `domain_agent_pools.py` | 327 | **P2** | core |  |
| 132 | `swarm_memory.py` | 323 | **P2** | core |  |
| 133 | `collaboration_hall.py` | 322 | **P2** | core |  |
| 134 | `browser_agent_automation.py` | 321 | **P2** | core |  |
| 135 | `context_manager.py` | 319 | **P2** | core |  |
| 136 | `error_envelope.py` | 316 | **P2** | core |  |
| 137 | `voice_engine.py` | 314 | **P2** | core |  |
| 138 | `connection_manager.py` | 313 | **P2** | core |  |
| 139 | `decision_engine.py` | 310 | **P2** | core |  |
| 140 | `skill_curator.py` | 308 | **P2** | core |  |
| 141 | `comfyui_gateway.py` | 307 | **P2** | core |  |
| 142 | `proactive_suggestions.py` | 302 | **P2** | core |  |
| 143 | `themes.py` | 302 | **P2** | core |  |
| 144 | `onboarding.py` | 301 | **P2** | core |  |
| 145 | `external_agent.py` | 294 | **P2** | core |  |
| 146 | `session_goals.py` | 283 | **P2** | core |  |
| 147 | `trajectory_compressor.py` | 282 | **P2** | core |  |
| 148 | `email_manager.py` | 279 | **P2** | core |  |
| 149 | `quality_judge.py` | 275 | **P2** | core |  |
| 150 | `opencode_client.py` | 272 | **P2** | core |  |
| 151 | `adaptive_router.py` | 262 | **P2** | core |  |
| 152 | `notes_calendar.py` | 258 | **P2** | core |  |
| 153 | `communication.py` | 252 | **P2** | core |  |
| 154 | `connectivity.py` | 249 | **P2** | core |  |
| 155 | `webhook_manager.py` | 249 | **P2** | core |  |
| 156 | `claude_headless.py` | 248 | **P2** | core |  |
| 157 | `prompt_assembler.py` | 248 | **P2** | core |  |
| 158 | `runtime.py` | 246 | **P2** | core |  |
| 159 | `token_budget.py` | 245 | **P2** | core |  |
| 160 | `time_tracker.py` | 243 | **P2** | core |  |
| 161 | `planning_engine.py` | 242 | **P2** | core |  |
| 162 | `credential_pool.py` | 238 | **P2** | core |  |
| 163 | `reasoning_strategies.py` | 238 | **P2** | core |  |
| 164 | `notification_center.py` | 237 | **P2** | core |  |
| 165 | `session_persistence.py` | 237 | **P2** | core |  |
| 166 | `memory_triage.py` | 229 | **P2** | core |  |
| 167 | `response_builder.py` | 229 | **P2** | core |  |
| 168 | `graph_evolution.py` | 228 | **P2** | core |  |
| 169 | `think_scrubber.py` | 228 | **P2** | core |  |
| 170 | `command_protocol.py` | 225 | **P2** | core |  |
| 171 | `risk_assessor.py` | 218 | **P2** | core |  |
| 172 | `extension_pipeline.py` | 216 | **P2** | core |  |
| 173 | `o1_indexing.py` | 215 | **P2** | core |  |
| 174 | `slash_commands.py` | 214 | **P2** | core |  |
| 175 | `compare_mode.py` | 211 | **P2** | core |  |
| 176 | `local_tool_calling.py` | 209 | **P2** | core |  |
| 177 | `library.py` | 207 | **P2** | core |  |
| 178 | `topic_analyzer.py` | 206 | **P2** | core |  |
| 179 | `ofp.py` | 205 | **P2** | core |  |
| 180 | `human_layer.py` | 198 | **P2** | core |  |
| 181 | `log_curator.py` | 198 | **P2** | core |  |
| 182 | `registry.py` | 192 | **P2** | core |  |
| 183 | `live_notes.py` | 191 | **P2** | core |  |
| 184 | `prompt_manager.py` | 191 | **P2** | core |  |
| 185 | `schema_sanitizer.py` | 189 | **P2** | core |  |
| 186 | `plugin_discovery.py` | 188 | **P2** | core |  |
| 187 | `prepare_nexus_datasets.py` | 187 | **P2** | core |  |
| 188 | `auto_test.py` | 185 | **P2** | core |  |
| 189 | `task_heartbeat.py` | 185 | **P2** | core |  |
| 190 | `audit_timeline.py` | 180 | **P2** | core |  |
| 191 | `tool_base.py` | 179 | **P2** | core |  |
| 192 | `muon_trainer.py` | 177 | **P2** | core |  |
| 193 | `code_absorber.py` | 176 | **P2** | core |  |
| 194 | `background_review.py` | 171 | **P2** | core |  |
| 195 | `memory_health.py` | 171 | **P2** | core |  |
| 196 | `skill_marketplace.py` | 171 | **P2** | core |  |
| 197 | `custom_commands.py` | 168 | **P2** | core |  |
| 198 | `chat_organizer.py` | 165 | **P2** | core |  |
| 199 | `midelware_pipeline.py` | 164 | **P2** | core |  |
| 200 | `pointer_store.py` | 163 | **P2** | core |  |
| 201 | `auto_commit.py` | 162 | **P2** | core |  |
| 202 | `snapshot_store.py` | 159 | **P2** | core |  |
| 203 | `daemon_runtime.py` | 151 | **P2** | core |  |
| 204 | `reflection_pattern.py` | 151 | **P2** | core |  |
| 205 | `agency_catalog.py` | 147 | **P2** | core |  |
| 206 | `wait_for_helper.py` | 143 | **P2** | core |  |
| 207 | `personas.py` | 141 | **P2** | core |  |
| 208 | `provider_discovery.py` | 135 | **P2** | core |  |
| 209 | `tool_monitor.py` | 134 | **P2** | core |  |
| 210 | `agent_handoff.py` | 131 | **P2** | core |  |
| 211 | `goal_detector.py` | 124 | **P2** | core |  |
| 212 | `producer_verifier_loop.py` | 124 | **P2** | core |  |
| 213 | `human_in_the_loop.py` | 116 | **P2** | core |  |
| 214 | `multi_signal_retrieval.py` | 115 | **P2** | core |  |
| 215 | `nexus.py` | 114 | **P2** | core |  |
| 216 | `ref_system.py` | 114 | **P2** | core |  |
| 217 | `retry_manager.py` | 112 | **P2** | core |  |
| 218 | `token_monitor.py` | 110 | **P2** | core |  |
| 219 | `provider_capabilities.py` | 101 | **P2** | core |  |
| 220 | `checkpoint_metrics.py` | 98 | **P2** | core | Checkpoint Contract |
| 221 | `protocol_router.py` | 97 | **P2** | core |  |
| 222 | `prompt_loader.py` | 93 | **P2** | core |  |
| 223 | `error_compactor.py` | 92 | **P2** | core |  |
| 224 | `perplexity_scorer.py` | 91 | **P2** | core |  |
| 225 | `antigravity_headless.py` | 86 | **P2** | core |  |
| 226 | `rate_limiter.py` | 86 | **P2** | core |  |
| 227 | `db_wal.py` | 79 | **P2** | core |  |
| 228 | `error_translation.py` | 78 | **P2** | core |  |
| 229 | `prompt_caching.py` | 78 | **P2** | core |  |
| 230 | `tool_security.py` | 73 | **P2** | core |  |
| 231 | `vision_config.py` | 67 | **P2** | core |  |
| 232 | `bounded_history.py` | 65 | **P2** | core |  |
| 233 | `incognito.py` | 62 | **P2** | core |  |
| 234 | `brain_service.py` | 58 | **P2** | core |  |
| 235 | `vram_router.py` | 56 | **P2** | core |  |
| 236 | `nexus_config.py` | 43 | **P2** | core |  |
| 237 | `path_normalize.py` | 34 | **P2** | core |  |
| 238 | `interfaces/` | 0 | **P2** | core subdir |  |
| 239 | `security/` | 0 | **P2** | core subdir |  |

---
**Autopsia patrones absorbidos:** Checkpoint Contract+SwarmBrief (hermes-workspace), CapabilitySet (openfang), 6 tipos memoria+Topic upserts+FTS5 (openakita/engram), MCP fallback+autodiscovery (odysseus/aden-hive), DMN/Notification scoring (lethe), Thompson sampling (ruflo), OFP HMAC-SHA256 (openfang-wire).
