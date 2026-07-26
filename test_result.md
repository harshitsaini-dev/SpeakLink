#====================================================================================================
# START - Testing Protocol - DO NOT EDIT OR REMOVE THIS SECTION
#====================================================================================================

# THIS SECTION CONTAINS CRITICAL TESTING INSTRUCTIONS FOR BOTH AGENTS
# BOTH MAIN_AGENT AND TESTING_AGENT MUST PRESERVE THIS ENTIRE BLOCK

# Communication Protocol:
# If the `testing_agent` is available, main agent should delegate all testing tasks to it.
#
# You have access to a file called `test_result.md`. This file contains the complete testing state
# and history, and is the primary means of communication between main and the testing agent.
#
# Main and testing agents must follow this exact format to maintain testing data. 
# The testing data must be entered in yaml format Below is the data structure:
# 
## user_problem_statement: {problem_statement}
## backend:
##   - task: "Task name"
##     implemented: true
##     working: true  # or false or "NA"
##     file: "file_path.py"
##     stuck_count: 0
##     priority: "high"  # or "medium" or "low"
##     needs_retesting: false
##     status_history:
##         -working: true  # or false or "NA"
##         -agent: "main"  # or "testing" or "user"
##         -comment: "Detailed comment about status"
##
## frontend:
##   - task: "Task name"
##     implemented: true
##     working: true  # or false or "NA"
##     file: "file_path.js"
##     stuck_count: 0
##     priority: "high"  # or "medium" or "low"
##     needs_retesting: false
##     status_history:
##         -working: true  # or false or "NA"
##         -agent: "main"  # or "testing" or "user"
##         -comment: "Detailed comment about status"
##
## metadata:
##   created_by: "main_agent"
##   version: "1.0"
##   test_sequence: 0
##   run_ui: false
##
## test_plan:
##   current_focus:
##     - "Task name 1"
##     - "Task name 2"
##   stuck_tasks:
##     - "Task name with persistent issues"
##   test_all: false
##   test_priority: "high_first"  # or "sequential" or "stuck_first"
##
## agent_communication:
##     -agent: "main"  # or "testing" or "user"
##     -message: "Communication message between agents"

# Protocol Guidelines for Main agent
#
# 1. Update Test Result File Before Testing:
#    - Main agent must always update the `test_result.md` file before calling the testing agent
#    - Add implementation details to the status_history
#    - Set `needs_retesting` to true for tasks that need testing
#    - Update the `test_plan` section to guide testing priorities
#    - Add a message to `agent_communication` explaining what you've done
#
# 2. Incorporate User Feedback:
#    - When a user provides feedback that something is or isn't working, add this information to the relevant task's status_history
#    - Update the working status based on user feedback
#    - If a user reports an issue with a task that was marked as working, increment the stuck_count
#    - Whenever user reports issue in the app, if we have testing agent and task_result.md file so find the appropriate task for that and append in status_history of that task to contain the user concern and problem as well 
#
# 3. Track Stuck Tasks:
#    - Monitor which tasks have high stuck_count values or where you are fixing same issue again and again, analyze that when you read task_result.md
#    - For persistent issues, use websearch tool to find solutions
#    - Pay special attention to tasks in the stuck_tasks list
#    - When you fix an issue with a stuck task, don't reset the stuck_count until the testing agent confirms it's working
#
# 4. Provide Context to Testing Agent:
#    - When calling the testing agent, provide clear instructions about:
#      - Which tasks need testing (reference the test_plan)
#      - Any authentication details or configuration needed
#      - Specific test scenarios to focus on
#      - Any known issues or edge cases to verify
#
# 5. Call the testing agent with specific instructions referring to test_result.md
#
# IMPORTANT: Main agent must ALWAYS update test_result.md BEFORE calling the testing agent, as it relies on this file to understand what to test next.

#====================================================================================================
# END - Testing Protocol - DO NOT EDIT OR REMOVE THIS SECTION
#====================================================================================================



#====================================================================================================
# Testing Data - Main Agent and testing sub agent both should log testing data below this section
#====================================================================================================

user_problem_statement: "Integrate the process-local Receiver connection-source inventory into the current legacy-authenticated WebSocket lifecycle without enabling hashed authentication."
backend:
  - task: "Safe isolated backend smoke tests"
    implemented: true
    working: true
    file: "backend/tests/test_smoke.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: "NA"
        agent: "main"
        comment: "Added in-process smoke coverage using a pytest temporary SQLite database; validation is pending."
      - working: false
        agent: "main"
        comment: "Initial collection failed because the existing virtual environment does not have httpx installed; no test executed and the real database was not used."
      - working: true
        agent: "main"
        comment: "Reworked the harness to use an ephemeral loopback Uvicorn process and requests. Result: 6 passed with 3 dependency/deprecation warnings."
  - task: "Guard legacy integration tests"
    implemented: true
    working: true
    file: "backend/tests/backend_test.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: "NA"
        agent: "main"
        comment: "Removed the remote fallback and added explicit URL, isolated-database, non-local opt-in, and environment credential gates; validation is pending."
      - working: true
        agent: "main"
        comment: "No-target collection skipped the module, a non-local target without opt-in was refused at collection, and the full tests directory completed with 6 passed and 1 skipped."
  - task: "Pure receiver status and acknowledgement contract"
    implemented: true
    working: true
    file: "backend/receiver_contract.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase: contract tests failed during collection because receiver_contract did not exist; no server, network, or database was used."
      - working: false
        agent: "main"
        comment: "First implementation run reached 30 passed and 1 failed because stopped.reason was overconstrained as mandatory."
      - working: true
        agent: "main"
        comment: "Made stopped.reason explicitly optional and bounded. Pure contract result: 31 passed in 0.14 seconds; broader regression validation is pending."
      - working: true
        agent: "main"
        comment: "Expanded allowed-transition and sequence-boundary coverage. Final pure unit result: 38 passed in 0.17 seconds. Full backend result: 44 passed, 1 skipped, 3 warnings in 2.86 seconds."
  - task: "Authenticated receiver acknowledgement integration"
    implemented: true
    working: true
    file: "backend/tests/test_receiver_ws_contract.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase: 13 isolated WebSocket cases failed at the first missing snapshot-manager seam. No Uvicorn, network socket, audio path, or real database was used."
      - working: true
        agent: "main"
        comment: "Implemented strict parsing, immutable per-store snapshots, ordering/deduplication/session checks, stale/offline handling, pending PLAY semantics, and meaningful transition persistence without a migration. Focused result after expanded malformed/stopped/error coverage: 15 passed with 3 warnings."
      - working: true
        agent: "main"
        comment: "Pure contract result: 38 passed. Complete isolated backend result: 59 passed, 1 skipped, 6 warnings. The guarded legacy module remained skipped and backend/speaklink_live.db was not used."
  - task: "Local non-audio receiver protocol simulator"
    implemented: true
    working: true
    file: "tools/receiver_simulator.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase: focused collection failed because tools.receiver_simulator did not exist. No server or database was accessed."
      - working: false
        agent: "main"
        comment: "First implementation run reached the real loopback scenario successfully; one safety-unit input omitted its explicit port and was correctly rejected before the intended non-loopback opt-in assertion."
      - working: true
        agent: "main"
        comment: "Focused simulator result: 3 passed in 2.35 seconds. Pure contract result: 38 passed. Complete isolated backend result: 62 passed, 1 skipped, 6 warnings in 3.77 seconds."
  - task: "Receiver WebSocket header authentication"
    implemented: true
    working: true
    file: "backend/tests/test_receiver_ws_auth.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase: 4 failed and 2 passed. Failures demonstrated the old token function signature, simulator URL concatenation, and acceptance of the legacy credential path."
      - working: true
        agent: "main"
        comment: "Focused header-auth result: 2 passed. Simulator result: 4 passed. Receiver contract plus WebSocket state result: 53 passed. Smoke result: 6 passed."
      - working: true
        agent: "main"
        comment: "Complete isolated backend result: 65 passed, 1 skipped, 8 warnings in 4.17 seconds. Failed authentication produced no snapshot, online state, health write, or credential-bearing response."
  - task: "Receiver credential lifecycle design and pure helpers"
    implemented: true
    working: true
    file: "backend/tests/test_receiver_credentials.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase: pure credential tests failed collection because receiver_credentials.py did not exist. No server, socket, environment secret, or database was accessed."
      - working: false
        agent: "main"
        comment: "Migration-specific red phase: the explicit legacy UUID-hex hashing helpers were absent; the new-token parser was intentionally not weakened."
      - working: true
        agent: "main"
        comment: "Pure credential lifecycle result: 12 passed in 0.03 seconds. Existing header-auth result: 2 passed with 3 warnings. Complete isolated backend result: 77 passed, 1 skipped, 8 warnings in 4.24 seconds."
  - task: "Receiver credential migration Phase 1"
    implemented: true
    working: true
    file: "backend/tests/test_receiver_credential_migration.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase: focused collection failed because migrations.py and the approved device-limit API did not exist. No server, socket, or database was accessed."
      - working: false
        agent: "main"
        comment: "First implementation run: 19 passed and one test-harness transaction-boundary check failed after read-only PRAGMAs; migration behavior itself was not the failure."
      - working: true
        agent: "main"
        comment: "Focused Phase 1 and pure credential result: 21 passed in 0.32 seconds using only pytest temporary SQLite databases. Broader regression validation is pending."
      - working: true
        agent: "main"
        comment: "Final focused result: 21 passed in 0.33 seconds. Receiver header-auth result: 2 passed with 3 warnings. Complete isolated backend result: 86 passed, 1 skipped, 8 warnings in 4.26 seconds. Python compilation succeeded."
  - task: "Isolated Receiver Device enrollment service Phase 2"
    implemented: true
    working: true
    file: "backend/tests/test_receiver_device_service.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase: focused collection failed because receiver_device_service.py did not exist. No server, socket, environment secret, or database was accessed."
      - working: true
        agent: "main"
        comment: "Focused isolated service result: 22 passed in 0.64 seconds. The service was then hardened so generation occurs under BEGIN IMMEDIATE and display names cannot persist credential-like input."
      - working: true
        agent: "main"
        comment: "Final focused service result: 26 passed in 0.81 seconds. Phase 1 migration and pure credential regression result: 21 passed in 0.32 seconds. Broader backend validation is pending."
      - working: true
        agent: "main"
        comment: "Final secret-scan focused result: 26 passed in 0.73 seconds. Receiver header-auth result: 2 passed with 3 warnings. Complete isolated backend result: 112 passed, 1 skipped, 8 warnings in 4.47 seconds. Python compilation succeeded."
  - task: "Isolated legacy Receiver Credential backfill rehearsal"
    implemented: true
    working: true
    file: "backend/tests/test_receiver_credential_backfill.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase: focused collection failed because receiver_credential_backfill.py did not exist. No server, socket, environment secret, or database was accessed."
      - working: true
        agent: "main"
        comment: "Initial focused rehearsal result: 14 passed in 0.55 seconds using only generated credentials/keys and pytest temporary SQLite files."
      - working: true
        agent: "main"
        comment: "Hardened focused result: 19 passed in 0.62 seconds, including unknown state, invalid key/time, replay audit validation, rollback, concurrency, and protected-path refusal. Broader regression validation is pending."
      - working: true
        agent: "main"
        comment: "Final results: focused backfill 19 passed in 0.62 seconds; Phase 1/credential/enrollment regressions 47 passed in 0.78 seconds; receiver header auth 2 passed with 3 warnings in 1.01 seconds; complete backend 131 passed, 1 skipped, 8 warnings in 4.78 seconds; compilation succeeded."
  - task: "Isolated Receiver Credential dual-verification service"
    implemented: true
    working: true
    file: "backend/tests/test_receiver_auth_service.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase: focused collection failed because receiver_auth_service.py did not exist. No server, socket, environment secret, or database connection was used."
      - working: true
        agent: "main"
        comment: "Focused isolated authentication result after mapping, redaction, and import-boundary hardening: 32 passed in 1.18 seconds using only generated test credentials/keys and pytest temporary SQLite files."
      - working: true
        agent: "main"
        comment: "Credential lifecycle regressions: 66 passed in 1.19 seconds. Existing Receiver WebSocket authentication and acknowledgement-contract regressions: 17 passed with 5 existing warnings in 1.38 seconds. Complete backend validation is pending."
      - working: true
        agent: "main"
        comment: "Final results: focused authentication 32 passed in 1.18 seconds; credential lifecycle regressions 66 passed in 1.18 seconds; Receiver WebSocket regressions 17 passed with 5 warnings in 1.47 seconds; complete backend 163 passed, 1 skipped, 8 warnings in 5.36 seconds."
  - task: "Isolated Receiver migration-state transition rehearsal"
    implemented: true
    working: true
    file: "backend/tests/test_receiver_migration_transition_service.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase: focused collection failed because receiver_migration_transition_service.py did not exist. No server, socket, environment secret, or database connection was used."
      - working: true
        agent: "main"
        comment: "Initial focused implementation result: 41 passed in 1.30 seconds using generated test keys and pytest temporary SQLite files only."
      - working: false
        agent: "main"
        comment: "One hardening run exposed a test-fixture SQL interpolation error after duplicate-token coverage was added; no service behavior or persistent data was involved."
      - working: true
        agent: "main"
        comment: "Hardened focused result: 49 passed in 1.57 seconds, including an explicit active-Device-without-Credential readiness case. Lifecycle service regressions: 98 passed in 2.12 seconds. Existing Receiver WebSocket tests: 17 passed with 5 warnings in 1.36 seconds. Complete backend validation is pending."
      - working: true
        agent: "main"
        comment: "Final complete backend result: 212 passed, 1 skipped, 8 existing dependency/deprecation warnings in 5.87 seconds. The protected database metadata remained unchanged."
  - task: "Pure active Receiver connection source inventory"
    implemented: true
    working: true
    file: "backend/tests/test_receiver_connection_inventory.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase: focused collection failed because receiver_connection_inventory.py did not exist. No database, server, socket, environment secret, or network connection was used."
      - working: false
        agent: "main"
        comment: "The first complete contract run reached 64 passed and one test assertion mismatch: Python 3.12 reports TypeError when adding an unknown attribute to a frozen slotted dataclass. Inventory behavior remained immutable."
      - working: true
        agent: "main"
        comment: "Focused pure inventory result: 65 passed in 0.15 seconds, covering source identity, bounded capacity, deterministic concurrency, immutable snapshots, process-local restart behavior, and transition-summary construction. Broader regression validation was pending."
      - working: true
        agent: "main"
        comment: "Final results: focused inventory rerun 65 passed in 0.19 seconds; authentication/transition regressions 81 passed in 2.49 seconds; lifecycle regressions 66 passed in 1.24 seconds; Receiver WebSocket regressions 17 passed with 5 existing warnings in 1.42 seconds; final complete backend 277 passed, 1 skipped, and 8 existing warnings in 6.12 seconds. Python compilation and diff checks succeeded."
  - task: "Legacy Receiver WebSocket connection inventory integration"
    implemented: true
    working: true
    file: "backend/tests/test_receiver_connection_inventory_ws.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase: the initial focused test failed because WSManager had no owned connection inventory. The expanded pre-implementation suite reported 19 failures for the missing ownership, connection-ID, summary, and lifecycle integration."
      - working: false
        agent: "main"
        comment: "The first implementation run reached 17 passed and 2 test-input failures: the capacity case observed the old fixture manager and receiver_ready omitted its two required check fields. Runtime inventory behavior was not the cause."
      - working: true
        agent: "main"
        comment: "Focused legacy WebSocket inventory integration result: 19 passed with 3 existing warnings in 1.07 seconds. Full regression validation is pending."
      - working: false
        agent: "main"
        comment: "The first complete xdist suite run reported 294 passed and 2 pure-service boundary failures because the focused test imported ws_manager during collection on both workers. Runtime behavior passed; the focused test import boundary required correction."
      - working: true
        agent: "main"
        comment: "Final results after explicit cancellation and metadata-redaction coverage: focused runtime integration 20 passed with 3 warnings in 1.10 seconds; pure inventory 65 passed in 0.12 seconds; authentication/transition regressions 81 passed in 2.40 seconds; existing Receiver WebSocket regressions 17 passed with 5 warnings in 1.38 seconds; complete backend 297 passed, 1 skipped, and 10 existing warnings in 7.08 seconds."
  - task: "Explicit Receiver dual-authentication runtime boundary"
    implemented: true
    working: true
    file: "backend/tests/test_receiver_dual_auth_runtime.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase: focused collection failed because receiver_runtime_auth.py did not exist. No production database, network socket, or migration was used."
      - working: false
        agent: "main"
        comment: "The first expanded run exposed two isolated-fixture API/time assumptions: enrollment returns public rather than row IDs, and the fixed issuance time was later than machine UTC. No runtime credential logic failed."
      - working: true
        agent: "main"
        comment: "Focused runtime-boundary result: 33 passed with 3 existing dependency/deprecation warnings in 1.31 seconds. Inventory regressions: 85 passed with 3 warnings. Existing Receiver WebSocket regressions: 17 passed with 5 warnings. Complete regression validation is pending."
      - working: true
        agent: "main"
        comment: "Final results: focused boundary 33 passed with 3 warnings in 1.33 seconds; authentication/transition regressions 81 passed in 2.68 seconds; lifecycle regressions 66 passed in 1.31 seconds; inventory regressions 85 passed with 3 warnings; existing Receiver WebSocket regressions 17 passed with 5 warnings; complete backend 330 passed, 1 skipped, and 12 existing warnings in 6.82 seconds. Python compilation succeeded."
      - working: true
        agent: "main"
        comment: "Final hardening rerun after bounded legacy input and generic Store-reconciliation failure handling: focused boundary 34 passed with 3 warnings in 1.33 seconds; complete backend 331 passed, 1 skipped, and 12 existing warnings in 7.11 seconds. Python compilation succeeded."
  - task: "Isolated controlled Receiver credential cutover rehearsal"
    implemented: true
    working: true
    file: "backend/tests/test_receiver_cutover_rehearsal.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase: focused collection failed because receiver_cutover_rehearsal.py did not exist. No server, socket, database, or secret was accessed."
      - working: false
        agent: "main"
        comment: "The first implementation run reached 4 passed and 3 test-harness assertion mismatches involving earlier key rejection, legitimate hashed-count text, and post-accept capacity close semantics. No transition or runtime rule failed."
      - working: true
        agent: "main"
        comment: "Focused cutover rehearsal: 7 passed with 21 dependency/deprecation warnings in 1.91 seconds using temporary SQLite, generated keys/credentials, a random loopback port, and one Uvicorn worker. Runtime/inventory regressions: 119 passed with 5 warnings; authentication/transition: 81 passed; lifecycle: 66 passed; existing WebSocket: 17 passed with 5 warnings. Complete validation is pending."
      - working: true
        agent: "main"
        comment: "Final results: focused rehearsal 7 passed with 21 warnings in 2.01 seconds; runtime/inventory regressions 119 passed with 5 warnings in 1.92 seconds; authentication/transition 81 passed in 2.55 seconds; lifecycle 66 passed in 1.25 seconds; existing WebSocket 17 passed with 5 warnings in 1.54 seconds; complete backend 338 passed, 1 skipped, and 32 warnings in 6.89 seconds. Python compilation succeeded."
  - task: "Receiver production cutover runbook and HMAC key-custody policy"
    implemented: true
    working: true
    file: "backend/tests/test_receiver_production_runbook.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase: all 9 pure document-contract tests failed because RECEIVER_PRODUCTION_CUTOVER_RUNBOOK.md and RECEIVER_HMAC_KEY_CUSTODY.md did not exist. No application, database, migration, key, socket, or network operation ran."
      - working: false
        agent: "main"
        comment: "The first documentation run reached 7 passed and 2 wording-contract failures, followed by 8 passed and 1 wrapping failure. The required policy was present; exact contract phrases were split across Markdown lines."
      - working: true
        agent: "main"
        comment: "Focused pure documentation contract result: 9 passed in 0.03 seconds. Broader cutover, transition, runtime, inventory, and complete backend regression validation is pending."
      - working: true
        agent: "main"
        comment: "Final results: document contract 9 passed in 0.03 seconds; cutover/transition regressions 56 passed with 21 existing warnings in 3.15 seconds; runtime/inventory regressions 54 passed with 5 existing warnings in 1.63 seconds; complete backend 347 passed, 1 skipped, and 32 existing warnings in 6.63 seconds. No runtime Python or frontend file changed, and no production database, key, migration, backup, cutover, or Receiver operation was performed."
  - task: "Receiver hosting/key-storage security-operations review and ADR"
    implemented: true
    working: true
    file: "backend/tests/test_receiver_security_operations_docs.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase (2026-07-25): 14 of 15 pure document-contract tests failed because RECEIVER_HOSTING_KEY_STORAGE_ADR.md and RECEIVER_SECURITY_OPERATIONS_REVIEW.md did not exist yet; the one passing test was the runtime-file fingerprint guard, which needs no document. No application, database, migration, key, socket, or network operation ran."
      - working: true
        agent: "main"
        comment: "First full-content run reached 12 passed and 3 wording-contract failures: the ADR's decision-drivers bullet used the word 'pricing' (tripping the no-vendor-pricing check) and the security review had not yet used the exact literal status-axis terms (READY) or the exact 'No real HMAC key was loaded' / 'No real database was opened, copied, or modified' phrases. No application, database, or Receiver behavior was involved."
      - working: true
        agent: "main"
        comment: "Focused pure documentation-contract result: 15 passed in 0.04 seconds. Existing production-runbook contract regressions: 9 passed. Cutover/transition regressions: 56 passed with 21 existing warnings (one real-loopback WebSocket test, test_full_loopback_forward_cutover_and_rollback, failed once under xdist contention and passed on every other rerun in isolation and in the full suite; this is pre-existing timing flakiness in a real-socket integration test, not a regression, since no runtime file was changed). Runtime/inventory regressions: 54 passed with 5 existing warnings. Complete backend suite: 362 passed, 1 skipped, 32 existing warnings in ~7 seconds on a clean rerun. Python compilation and `git diff --check` succeeded. No runtime Python file, frontend file, or `backend/speaklink_live.db` was touched; no real HMAC key, Windows service, ACL, or TLS configuration was created."
  - task: "Canonical Zone and Store catalog (9 Zones / 44 Stores)"
    implemented: true
    working: true
    file: "backend/tests/test_store_catalog.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase (2026-07-25 UTC, branch feat/canonical-store-zone-catalog, commit before work af168aa): focused collection failed with ModuleNotFoundError: No module named 'store_catalog'. No application, protected database, migration, key, socket or network operation ran."
      - working: false
        agent: "main"
        comment: "Real regression found and fixed honestly. The first seed_stores implementation was an insert-if-missing reconciler, so it injected all 44 canonical Stores into any non-empty database. That broke the isolated runtime fixtures, which build a controlled 3-Store fleet with complete backfilled credential mappings: test_receiver_dual_auth_runtime.py and neighbours reported 11 failed and 34 errors with 'NOT NULL constraint failed: stores.receiver_token'. Root cause was the seeding design, not the existing tests. seed_stores was changed to a first-run bootstrap that returns immediately when the Store table is non-empty, which is also the safer production behaviour because startup can never mutate an existing fleet. Two of the new catalog tests were updated to assert that safer contract."
      - working: true
        agent: "main"
        comment: "Focused catalog result: 22 passed. Receiver WebSocket/auth/inventory regressions: 71 passed with 9 existing warnings. Credential/migration regressions: 53 passed. Isolated smoke: 6 passed with 3 existing warnings. Complete backend suite: 384 passed, 1 skipped, 32 existing warnings in 7.95 seconds (previous baseline 362 passed + 22 new catalog tests). The known real-loopback WebSocket test test_full_loopback_forward_cutover_and_rollback passed in this run. python -m compileall -q backend succeeded. Frontend: yarn test --watchAll=false --passWithNoTests reported 'No tests found, exiting with code 0' because the project has no frontend test files; CI=false yarn build reported 'Compiled successfully.' git diff --check reported no whitespace errors. Protected database backend/speaklink_live.db metadata was identical before and after: 487424 bytes, LastWriteTimeUtc 2026-07-24 08:48:46; it was never opened, queried, migrated, seeded or modified. All tests used pytest temporary SQLite databases only."
  - task: "Catalog contract realignment and Receiver replacement handover fix"
    implemented: true
    working: true
    file: "backend/tests/test_receiver_replacement_race.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Preflight blocker (2026-07-25 UTC, branch feat/canonical-store-zone-catalog). HEAD was d29e18e 'Correct Store names in seed file', not the expected e8b75dd; the operator had corrected 14 Store codes/names (UN Old->UN, UN ASR->ASR, Vikaspuri New Store/VP New->Vikaspuri New/VP2, RRPL New Rajapuri/RRPL RP->RRPL/RRPL, JHA6->JHA, JHA6 New->JHA2, ME DP->RMME, ME New->ME3, RG New->RG2, Vishnu Garden 2->Vishnu Garden New, Bhogal CR->RMCR, Noida->NS104, GZB->GZBD, NIT 1 Faridabad/NIT1->NIT Faridabad/NIT). The catalog contract test caught it exactly as designed: 3 failed, 381 passed, 1 skipped. Work stopped before editing and the mismatch was reported. Protected database mtime had also moved from 24/07 08:48:46 to 25/07 11:12:47 with size unchanged at 487424; the operator confirmed this was a normal backend startup, not a task command."
      - working: false
        agent: "main"
        comment: "After realigning EXPECTED_CATALOG to the corrected codes the focused suite reached 22 passed, but the full suite became approximately 50% flaky (2 of 4 runs failed) at test_receiver_cutover_rehearsal.py:799 with 'offline' != 'online'. Measured at HEAD without the change: 4 of 4 runs passed. The change was therefore not dismissed as unrelated flakiness; it had unmasked latent defects by shifting xdist worker timing away from fail-fast catalog tests."
      - working: false
        agent: "main"
        comment: "Two distinct latent defects were isolated. First, WSManager.connect_receiver awaited old.close() while the outgoing socket was still installed as the Store's current connection, and disconnect_receiver is synchronous and takes no lock, so the old handler's finally block could claim currency and drive a status='offline' write for a Store that had just received a healthy replacement. A first attempt that popped the old entry before the close was rejected: it introduced a transient window where get_receiver_connection_id returned None mid-handover, which broke the cutover test's polling and could surface as a spurious dashboard offline. Second, test_receiver_cutover_rehearsal.py waited only for the connection inventory to empty after closing a socket, but the server removes the inventory record before writing Store health, so the test could capture a stale 'online' row that the pending 'offline' write then invalidated mid-assertion."
      - working: true
        agent: "main"
        comment: "Final implementation: WSManager tracks _superseded_connection_ids, marking a connection before its close is awaited and clearing it once the inventory record is removed; disconnect_receiver returns False for a marked ID. Existing capacity-rejection semantics and the no-gap current-connection-ID behaviour are preserved. backend/tests/test_receiver_replacement_race.py forces the interleaving deterministically (4 tests, no SQLite, no socket, no credentials) and imports ws_manager lazily so it cannot break other suites' sys.modules purity assertions under --dist loadscope. The cutover test's wait now also requires the offline health write to land. Results: focused race tests 4 passed; focused catalog 22 passed; WebSocket/inventory/cutover/auth regressions 78 passed; complete backend suite 388 passed, 1 skipped, 32 existing warnings, green across 8 consecutive runs (previously approximately 50% flaky). python -m compileall -q backend succeeded and git diff --check was clean. No frontend file changed. Protected database backend/speaklink_live.db was never opened, queried, migrated, seeded or modified; all database tests used pytest temporary files."
  - task: "Read-only Store catalog reconciliation report"
    implemented: true
    working: true
    file: "backend/tests/test_store_catalog_reconciliation.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Test-first red phase (2026-07-25 UTC). Starting branch feat/canonical-store-zone-catalog at commit d025f886f72f80671f338b05fbf68449ae56a30f, upstream matching, working tree clean, baseline suite 388 passed / 1 skipped / 32 warnings. New branch feat/store-catalog-reconciliation-report. Focused collection failed with ModuleNotFoundError: No module named 'store_catalog_reconciliation'. No application, protected database, migration, key, socket or network operation ran."
      - working: false
        agent: "main"
        comment: "Two genuine test-harness defects were found and fixed rather than worked around. First, the helper called Base.metadata.create_all without importing models, so no tables were created and 26 tests failed with a misleading 'no stores table' schema error. Second, the duplicate-code test could not insert duplicates because the live schema declares UNIQUE(store_code); that is a real schema fact, so the test was split into a duplicate-full-name case against the real schema (store_name has no unique constraint) and a duplicate-code case against a deliberately relaxed hand-built schema, documenting why."
      - working: false
        agent: "main"
        comment: "The full suite reported 1 failed / 428 passed because test_report_does_not_import_or_execute_runtime_startup asserted global sys.modules purity. Under pytest-xdist --dist loadscope the worker is shared with suites that legitimately import FastAPI, so the assertion measured the worker rather than the module. It was rewritten to run reconcile() in a clean subprocess and assert that server, fastapi, uvicorn, starlette and ws_manager are all absent from that interpreter, which actually proves the module's own import graph."
      - working: true
        agent: "main"
        comment: "Focused reconciliation result: 41 passed. Catalog/race/cutover regressions: 33 passed with 21 existing warnings. Smoke, migration, device, WebSocket auth/contract, inventory and dual-auth regressions: 111 passed with 11 existing warnings. python -m compileall -q backend succeeded. Complete backend suite: 429 passed, 1 skipped, 32 existing warnings, green across 5 consecutive runs (baseline 388 plus 41 new tests). git diff --check clean. Protected database backend/speaklink_live.db metadata was identical before and after: 487424 bytes, LastWriteTimeUtc 2026-07-25 11:12:47. It was never opened, queried, copied, migrated, seeded or modified, and NO reconciliation was executed against real data. Every database test used a pytest temporary file-backed SQLite snapshot. Read-only enforcement is proven directly: a dedicated test asserts PRAGMA query_only is 1 and that UPDATE, DELETE and CREATE TABLE all raise sqlite3.OperationalError on the report connection, and separate tests assert the snapshot SHA-256, size, mtime, Store rows and dependent rows are unchanged. Protected-path refusal is proven for the exact path, a '..' path, a path relative to another working directory, and a same-file hard link (via an isolated stand-in, never the real database). Text and JSON output are asserted free of any credential marker. No frontend, runtime, authentication, WebSocket, seed or migration behaviour changed."
  - task: "Local one-Store pilot readiness harness"
    implemented: true
    working: true
    file: "backend/tests/test_local_pilot.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Preflight blocker (2026-07-26 UTC). Starting branch feat/store-catalog-reconciliation-report at b8f2292b2b9a7c21cdb3366f3f5de3b0aa719228, upstream matching, tree clean, baseline 429 passed / 1 skipped / 32 warnings. Two Uvicorn processes were running, both configured for port 8000: PID 6632 (system Python) held the listener and PID 11980 (repo venv) did not, so ownership was ambiguous. The React dev server was also running on 0.0.0.0:3000. Critically, the protected database had a live 910,552-byte -wal plus a -shm file dated 26/07 06:59, proving a live backend was writing to it. Work stopped before creating a branch and nothing was terminated. An honest completeness correction was recorded: earlier tasks verified only the .db file's size and mtime, not the WAL sidecar, so 'metadata unchanged' was true but incomplete evidence while WAL absorbed the writes."
      - working: false
        agent: "main"
        comment: "The operator stopped the processes. SQLite checkpointed the WAL into the main file on clean shutdown, so the protected database moved from 487424 bytes / 2026-07-25 11:12:47 to 507904 bytes / 2026-07-26 07:42:09 with both sidecars removed. That was the operator's own application shutting down, not a task command. New accepted baseline recorded, baseline suite re-verified at 429 passed / 1 skipped / 32 warnings, then branch test/local-one-store-pilot-readiness created. Test-first red phase: ModuleNotFoundError: No module named 'tools.local_pilot'."
      - working: false
        agent: "main"
        comment: "First implementation run reached 12 passed / 14 failed / 9 errors because the reconciliation tool correctly refused the freshly created pilot database: backend/db.py enables PRAGMA journal_mode=WAL, so a -wal sidecar sat beside the snapshot. This was the safety feature working, not a false positive. A checkpoint helper was added that runs PRAGMA wal_checkpoint(TRUNCATE) followed by PRAGMA journal_mode=DELETE, because TRUNCATE empties the WAL but leaves the file present. It runs only against the pilot database, after initialisation and after smoke shutdown, since Windows terminate() is an abrupt kill that cannot let SQLite clean up."
      - working: true
        agent: "main"
        comment: "Focused local-pilot result: 35 passed. Catalog and reconciliation regressions: 98 passed combined with the pilot suite. Receiver WebSocket auth/contract/inventory/replacement-race/cutover plus smoke: 54 passed with 29 existing warnings. python -m compileall -q backend tools succeeded. Complete backend suite: 464 passed, 1 skipped, 32 existing warnings, green across 5 consecutive runs (429 baseline plus 35 new). Frontend: yarn test --watchAll=false --passWithNoTests reported no test files exist; CI=false yarn build compiled successfully. Actual CLI prepare at commit b8f2292 produced an isolated database at %LOCALAPPDATA%/SpeakLink/local-pilot/data/speaklink_local_pilot.db, SHA-256 4e0710f859f08ec3cfef947d6797c01f5d52bd4922173cbbb951bf170e766dd4, 44 Stores, 9 Zones, demo_codes_present empty, reconciliation EXACT_CANONICAL_MATCH. Actual CLI smoke returned LOCAL_PILOT_SMOKE_PASSED on 127.0.0.1:61266 with exactly one Uvicorn worker: liveness ok, login ok, 44 Stores, 9 Zones, Store UN authenticated over the Bearer header, query-string credential refused, observed_connection CONNECTED, readiness/playback/acoustic all NOT_REPORTED, speaker_verified false, receiver cleanup ok, shutdown ok, no process left running and port 61266 released. Secret scan of backend.log, smoke-report.json, pilot-state.json and PILOT_ONLY: all clean; the pilot database itself contains the strings 'password' and 'receiver_token' only as SQLite column names, and it lives outside Git. Protected database metadata was 507904 bytes / 2026-07-26 07:42:09 before and after, with no sidecars; it was never opened, copied or modified. All pilot artifacts stayed outside the repository and none were staged."
  - task: "One-Store live-audio software pilot"
    implemented: true
    working: true
    file: "backend/tests/test_one_store_audio_pilot.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Preflight (2026-07-26 UTC). Starting branch test/local-one-store-pilot-readiness at 73d282682fc8aca15aaa91ec845928d77d7c30ad, upstream matching, tree clean, no SpeakLink process running, protected database 507904 bytes / 2026-07-26 07:42:09 with no WAL or SHM. Baseline suite 464 passed / 1 skipped / 32 warnings and the existing local pilot returned LOCAL_PILOT_SMOKE_PASSED. FFmpeg 8.1.2-full_build (gyan.dev, --enable-libopus) with encoders opus and libopus, decoders opus and libopus, and formats webm (mux) plus matroska,webm (demux). New branch feat/one-store-live-audio-software-pilot. Test-first red phase: ModuleNotFoundError: No module named 'audio_protocol'."
      - working: false
        agent: "main"
        comment: "Existing audio path inspected before coding. WSManager.fanout_audio awaited send_binary_to_receiver for each target Store in sequence with no queue and no bound, so one slow Receiver would stall every other Store; there was no prepare/READY gate, so audio could be fanned out to any connected Receiver as soon as a session went live. The React HQBroadcaster already produced audio/webm;codecs=opus, mono, 32 kbps, 250 ms chunks and already ignored empty chunks and stopped its tracks, so no format change was needed - only readiness gating."
      - working: false
        agent: "main"
        comment: "Three real defects were found and fixed rather than worked around. First, one queue-isolation test asserted the fast Store received all 10 chunks while the producer never yielded; with a synchronous burst every bounded queue legitimately overflows, so the test was corrected to yield between chunks, which is what a real 250 ms cadence does, and it now also asserts the fast Store dropped nothing while the slow Store did. Second, the first end-to-end run failed at POST /stop with HTTP 400 because closing the broadcaster socket first triggers the backend's existing broadcaster_disconnected safety net, which already ends the session; the orchestrator was corrected to issue the explicit stop while the socket is still open, exactly as the dashboard does. Third, the fixture was not byte-reproducible because the Matroska muxer writes a random SegmentUID and encoder metadata, so -bitexact and -map_metadata -1 were added; the fixture is now deterministic."
      - working: true
        agent: "main"
        comment: "Focused audio results: 29 protocol/queue tests and 23 audio-pilot tests, 52 passed. Local pilot, catalog and reconciliation regressions: 98 passed. Receiver WebSocket auth/contract/inventory/replacement-race/cutover plus smoke: 54 passed with 29 existing warnings. compileall backend tools succeeded. Complete backend suite: 516 passed, 1 skipped, 32 existing warnings, green across 5 consecutive runs (464 baseline plus 52 new). Frontend: yarn build compiled successfully and yarn test --passWithNoTests reported no test files exist, which is not behavioural frontend coverage. Deterministic fixture pilot-tone.webm: opus, mono, matroska/webm, 4.008 s, 22967 bytes, SHA-256 9fc6898d72dc7b82ff9e5b88f1fddddb8392bf84813d3fdf1e0604d8d4419e2d, stored outside Git. Actual end-to-end run returned ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED on 127.0.0.1:63167 with exactly one Uvicorn worker for Store UN: CONNECTED, READY, AUDIO_RECEIVING, PLAYBACK_CONFIRMED and STOPPED all observed; ffmpeg_returncode 0; ffmpeg_decoded_microseconds 4000000; 17 chunks and 22967 bytes sent and received with 0 dropped; sink_mode null; speaker_verified false. Cleanup verified: no backend, Receiver or FFmpeg process left and the port released. Secret scan of audio-backend.log, audio-receiver.log, audio-receiver-report.json, audio-smoke-report.json, pilot-state.json and PILOT_ONLY: all clean, confirming --no-access-log kept the query-string JWT out of the log; the pilot database matches 'password' and 'receiver_token' only as SQLite column names and lives outside Git. Protected database 507904 bytes / 2026-07-26 07:42:09 with no sidecars before and after; never opened, copied or modified. No manual browser microphone test was performed by this task."
  - task: "One-Store Windows output-device pilot"
    implemented: true
    working: true
    file: "backend/tests/test_windows_audio_output.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: false
        agent: "main"
        comment: "Preflight blocker (2026-07-26 UTC). Starting branch feat/one-store-live-audio-software-pilot at d4a8ba583887933675af7de638db8ca67a31b17a, upstream matching, tree clean, no pilot process running. The protected database differed from the handoff baseline: still 507904 bytes with no WAL or SHM, but LastWriteTimeUtc had moved from 2026-07-26 07:42:09 to 08:43:13. Work stopped before editing; the operator confirmed they had run the application, so the new baseline was accepted. Baseline suite 516 passed / 1 skipped / 32 warnings and the software audio smoke returned ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED. ffmpeg, ffprobe and ffplay 8.1.2 all present."
      - working: false
        agent: "main"
        comment: "Capability probe showed the task could not be completed with installed tools. The FFmpeg build has no audio output muxer at all: its full -devices list contains exactly one muxer, caca (ASCII-art video), with dshow and openal present only as capture demuxers. ffplay exposes only DirectShow capture options and plays to the SDL default. No Python audio library (sounddevice, pyaudio, soundcard, pygame, simpleaudio, miniaudio) was installed. Since Hard Safety Rule 4 forbids silently using the Windows default device, a small dependency was genuinely required. The operator approved sounddevice==0.5.2 (MIT, PortAudio V19.7.0). Installing it moved cffi 2.0.0 to 2.1.0 and added pycparser==3.0; backend/requirements.txt records all three. Test-first red phase: ModuleNotFoundError: No module named 'tools.windows_audio_devices'."
      - working: true
        agent: "main"
        comment: "Real device enumeration on this machine listed 8 output endpoints read-only, opening nothing and changing nothing: index:1 is the current default (an HDMI monitor), index:5 is a Bluetooth headset (flagged), and index:3 and index:4 share the identical name 'LG IPS QHD-1 (NVIDIA High Definition Audio)' under DirectSound and WASAPI, which is exactly why name-only selection is refused as ambiguous and a stable index:N selector is preferred. Focused results: 40 device/sink tests passed, every one against an injected fake backend so no automated test opens a real device or plays a sound. Audio protocol, queue and one-Store audio pilot: 92 passed combined. Local pilot, catalog and Receiver WebSocket regressions: 89 passed with 25 existing warnings. compileall backend tools succeeded. Null-sink audio smoke re-ran green with sink_mode null, ffmpeg_returncode 0, 0 dropped chunks and speaker_verified false, proving the default sink is unchanged. Frontend yarn build compiled and yarn test reported no test files, which is not behavioural coverage. Complete backend suite: 556 passed, 1 skipped, 32 existing warnings, green across 5 consecutive runs (516 baseline plus 40 new). NO real hardware test was performed and NO operator audible observation was recorded: the chime and browser checklists are left for the operator. Protected database 507904 bytes / 2026-07-26 08:43:13 with no sidecars before and after; never opened, copied or modified. No leftover python, node, ffmpeg or ffplay process; secret scan of all pilot logs and JSON reports clean. SPEAKER_VERIFIED remains unavailable and unclaimed."
  - task: "One-Store Windows output hardware validation"
    implemented: true
    working: true
    file: "ONE_STORE_WINDOWS_OUTPUT_VALIDATION_RESULT.md"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: true
        agent: "main"
        comment: "Preflight (2026-07-26 UTC). Starting branch feat/one-store-windows-output-pilot at 6d922b2dbc309bbb899221a0a016df77036cbaec, upstream matching, tree clean, no pilot process running, protected database 507904 bytes / 2026-07-26 08:43:13 with no WAL or SHM. Baseline complete suite 556 passed / 1 skipped / 32 warnings; compileall OK; frontend yarn build compiled and yarn test reported no test files; null-sink smoke returned ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED with sink_mode null. Versions: Python 3.12.10, FFmpeg/ffprobe 8.1.2, sounddevice 0.5.2, PortAudio V19.7.0, CFFI 2.1.0. New branch test/one-store-windows-output-hardware-validation."
      - working: false
        agent: "main"
        comment: "Outcome HARDWARE_PILOT_BLOCKED. The operator confirmed no amplifier and no wired path to one, so HARDWARE_PILOT_PASSED was impossible: it requires hearing audio through the intended amplifier/speaker path. The operator offered to test over Bluetooth TWS earbuds; that was recorded as an explicit scope deviation and NOT executed, because the task forbids using a Bluetooth endpoint and it could not have produced a pass. No sound was played, no audio device was ever opened, and no operator audible observation was obtained."
      - working: true
        agent: "main"
        comment: "Validation found four real defects before any sound was played, all fixed test-first with 25 new tests that use an injected fake audio backend so none can open a real device. (1) The sink hardcoded 48000 Hz / 1 channel and ignored the device; the real endpoint advertised 44100 Hz / 2 channels under WDM-KS, which is strict about formats, so the open could have failed or the audio been wrong. The sink now adopts the device sample rate and channel count capped at 2, and the FFmpeg command resamples and re-channels to match. (2) The test chime raised a raw EOFError traceback in a non-interactive shell; the confirmation gate correctly blocked playback but the failure is now a controlled refusal. (3) index:N was documented as a stable selector but is not: connecting a Bluetooth earbud set renumbered every device and moved the wired endpoint from index:7 to index:18, proven by two enumerations minutes apart. A verified selector index:N@ExactName was added that pins the index to the name, fails closed after a renumber and reports what is actually at that index. (4) Bluetooth detection missed A2DP endpoints: Headphones (Nirvana X TWS Stereo) was unflagged; the heuristic now also matches tws, a2dp, airpods, wireless, earbud and handsfree and prints wireless? so it reads as a hint. Results: focused Windows-output 65 passed; audio protocol and one-Store audio pilot 52 passed; compileall OK; frontend build compiled; null-sink smoke still ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED; complete backend suite 581 passed, 1 skipped, 32 existing warnings, green across 5 consecutive runs (556 baseline plus 25 new). Protected database 507904 bytes / 2026-07-26 08:43:13 with no sidecars before and after; never opened, copied or modified. No leftover python, node, ffmpeg or ffplay process and no listening pilot port. SPEAKER_VERIFIED remains NOT_IMPLEMENTED and the amplifier path remains unproven."
frontend: []
metadata:
  created_by: "main_agent"
  version: "1.0"
  test_sequence: 26
  run_ui: false
test_plan:
  current_focus: []
  stuck_tasks: []
  test_all: false
  test_priority: "high_first"
agent_communication:
  - agent: "main"
    message: "Run only the isolated smoke suite and safety/compile checks; do not target the old Emergent URL or the real SQLite database."
  - agent: "main"
    message: "Focused validation completed: Python compilation succeeded; isolated smoke tests passed; guarded integration tests did not contact a remote service."
  - agent: "main"
    message: "Pure contract unit tests are green. Run the preserved full backend suite next; do not integrate runtime behavior or access the real database."
  - agent: "main"
    message: "Receiver contract and full isolated backend validation completed. Runtime server, WebSocket manager, models, database schema, and frontend remain unchanged."
  - agent: "main"
    message: "Authenticated receiver integration is green in isolated tests. No frontend, model, migration, Receiver Agent, LinkGuard transport, audio stream, or production database change was made."
  - agent: "main"
    message: "The non-audio simulator passed real-loopback scenarios using generated credentials and a temporary database. It made no server, frontend, model, migration, FFmpeg, audio, or production database change."
  - agent: "main"
    message: "Receiver WebSocket header authentication is green. The legacy credential path was removed, the simulator uses Authorization Bearer, and browser compatibility was intentionally not weakened."
  - agent: "main"
    message: "Credential lifecycle design and pure helpers are complete. No model, schema, server, frontend, migration, or real database change was made; migration remains gated on design review."
  - agent: "main"
    message: "Phase 1 additive migration implementation is ready for isolated regression testing. Do not invoke it against backend/speaklink_live.db or enable runtime authentication changes."
  - agent: "main"
    message: "Phase 1 isolated validation is complete. The explicit runner remains disconnected from startup and refused the protected real database; Phase 2 backfill and dual verification remain unimplemented."
  - agent: "main"
    message: "The isolated Phase 2 enrollment service is ready for backend regression testing. It is not connected to FastAPI, startup, WebSockets, Store APIs, frontend, or the real database."
  - agent: "main"
    message: "Phase 2 service validation is complete. Enrollment remains isolated and legacy_only; no runtime authentication, backfill, migration-state, frontend, or protected-database change was made."
  - agent: "main"
    message: "The isolated legacy backfill rehearsal is ready for backend regression testing. It changes migration state only in temporary databases and does not enable dual verification or touch runtime authentication."
  - agent: "main"
    message: "Legacy backfill rehearsal validation is complete. The real database and production authentication remain untouched; temporary state reaches backfilled with legacy verification still enabled."
  - agent: "main"
    message: "The isolated read-only Receiver Credential verifier is ready for complete backend regression testing. It is not connected to FastAPI, WebSockets, runtime state, frontend, or the protected real database."
  - agent: "main"
    message: "Dual-verification service validation is complete. Authentication is read-only and remains isolated; production WebSocket authentication, runtime status, frontend, migration state, and the real database are unchanged."
  - agent: "main"
    message: "The isolated migration-state transition rehearsal is ready for complete backend validation. It changes only temporary state/audit rows and performs no runtime socket action or real-database migration."
  - agent: "main"
    message: "Migration-state transition validation is complete. Runtime authentication, live sockets, receiver snapshots, frontend, schema, and the protected database remain unchanged."
  - agent: "main"
    message: "The pure source-tagged inventory is ready for complete backend regression testing. It is not connected to FastAPI, WebSockets, authentication runtime, transition runtime, SQLite, frontend, or the protected database."
  - agent: "main"
    message: "Connection inventory validation is complete. Runtime and frontend files remain unchanged; focused tests required no database, and the protected database was not opened or modified."
  - agent: "main"
    message: "Legacy-only Receiver WebSocket inventory wiring is ready for complete backend regression testing. Hashed authentication, transition execution, schema changes, frontend behavior, and the protected database remain out of scope."
  - agent: "main"
    message: "Legacy-only runtime inventory validation is complete. Exact connection cleanup and summary counts are green; production authentication remains raw Store-token only and no migration transition or protected-database operation was added."
  - agent: "main"
    message: "The explicit runtime authentication boundary is ready for complete backend regression testing. The normal app remains legacy-only; migration-aware behavior requires an injected temporary engine/key ring, and no production cutover or transition API exists."
  - agent: "main"
    message: "Dual-authentication runtime-boundary validation is complete. The default application remains legacy-only; explicit temporary-app injection preserves canonical source IDs without credential-table writes or a production migration-state cutover."
  - agent: "main"
    message: "The isolated cutover rehearsal is ready for complete backend validation. It uses generated keys, temporary SQLite, one loopback worker, and the existing transition service; the default app, real database, and production migration state remain untouched."
  - agent: "main"
    message: "Controlled cutover rehearsal validation is complete. Forward and rollback state sequences, source blockers, key readiness, loopback handshakes, transactional rollback, and default legacy-only preservation are green; no production cutover occurred."
  - agent: "main"
    message: "The review-only production runbook and HMAC key-custody policy are ready for regression validation. Focused tests read Markdown only; no real database, key, migration, cutover, server, Receiver, or frontend operation is authorized."
  - agent: "main"
    message: "Production runbook validation is complete. Required phases, separate key/database recovery, source blockers, staged pilot, rollback, stop/abort controls, and status separation are documented; all regressions are green and no production operation occurred."
  - agent: "main"
    message: "The security/operations review and hosting/key-storage ADR are ready for regression validation (2026-07-25). Focused tests read Markdown only; the ADR is marked Proposed for pilot approval and selects a dedicated Windows Server/VM, one Uvicorn worker, a dedicated non-admin service identity, and DPAPI-protected key storage outside Git/SQLite. No real key, host, service account, TLS config, or database operation was performed."
  - agent: "main"
    message: "Security-operations-review/ADR validation is complete. Focused doc-contract tests, existing production-runbook tests, cutover/transition regressions, runtime/inventory regressions, and the complete backend suite are green (one real-loopback WebSocket test flaked once under parallel load and passed on every rerun, consistent with pre-existing timing sensitivity, not a regression). No runtime Python file, frontend file, or the protected database was touched."
  - agent: "main"
    message: "The canonical 9-Zone / 44-Store catalog is ready for regression validation. Zone is carried by the existing indexed Store.region field, so no schema change or migration was needed. The 13-entry demo SAMPLE_STORES list was removed from backend/seed.py; no frontend mock catalog existed because the dashboard already read /api/stores. Do not run catalog reconciliation against backend/speaklink_live.db: that needs a separate backup, dry run and approval."
  - agent: "main"
    message: "Canonical catalog validation is complete. Focused catalog 22 passed; WebSocket/auth/inventory regressions 71 passed; credential/migration regressions 53 passed; complete backend 384 passed, 1 skipped, 32 existing warnings; compileall succeeded; frontend build compiled successfully and no frontend test files exist. seed_stores is a first-run bootstrap only, so an existing fleet is never mutated at startup. The protected database metadata was unchanged, and the old demo rows it may still hold require a separate reviewed cleanup task."
  - agent: "main"
    message: "The Store Catalog Reconciliation Report was NOT started. Preflight found HEAD at d29e18e with 14 operator-corrected Store codes and 3 red catalog contract tests, which is a documented stop condition, so no reconciliation code exists yet. That task should be restarted from a clean, green baseline."
  - agent: "main"
    message: "Catalog contract realigned to the operator's corrected Store codes, and two latent defects that this unmasked are fixed: the Receiver replacement handover race in ws_manager, and a wait-condition gap in the cutover rehearsal test. The full backend suite is 388 passed, 1 skipped, 32 warnings and green across 8 consecutive runs. No frontend file changed and the protected database was never opened."
  - agent: "main"
    message: "The read-only Store catalog reconciliation report is ready for regression validation. It requires an explicitly supplied isolated snapshot, refuses backend/speaklink_live.db before opening any connection, and opens the snapshot with a SQLite mode=ro URI plus PRAGMA query_only. Do not run it against real data: that needs an operator-produced quiesced snapshot, and any cleanup remains a separate approved task."
  - agent: "main"
    message: "Reconciliation report validation is complete. Focused 41 passed; catalog/race/cutover 33 passed; smoke/migration/device/WebSocket regressions 111 passed; complete backend 429 passed, 1 skipped, 32 warnings, green across 5 consecutive runs; compileall succeeded. The protected database was never opened or modified, no reconciliation ran against real data, and no cleanup was executed. Documentation wording was corrected: commit d29e18e changed 14 catalog entries comprising 13 short names and 4 full names."
  - agent: "main"
    message: "The local one-Store pilot readiness harness is ready for regression validation. It prepares a disposable pilot database under %LOCALAPPDATA% outside Git, refuses the protected database and any repository-contained pilot root, and runs a loopback-only smoke test with exactly one Uvicorn worker. Do not treat a passing run as live-audio or speaker evidence."
  - agent: "main"
    message: "Local pilot validation is complete. Focused 35 passed; complete backend 464 passed, 1 skipped, 32 warnings across 5 consecutive runs; compileall succeeded; frontend build compiled and no frontend test files exist. The recorded smoke run reported CONNECTED with readiness, playback and acoustic all NOT_REPORTED and speaker_verified false. Status is READY_FOR_LOCAL_SOFTWARE_PILOT_TEST only: NOT ready for live audio, speakers or production. The protected database was never opened, copied or modified."
  - agent: "main"
    message: "The one-Store live-audio software pilot is ready for regression validation. It adds only the missing prepare control message, keeps the frozen receiver_contract for Receiver acknowledgements, gives each Store a bounded 24-chunk queue so a slow Receiver cannot stall the broadcaster, and decodes with real FFmpeg to a null sink. Do not read a pass as speaker, amplifier or output-device evidence."
  - agent: "main"
    message: "One-Store live-audio software pilot validation is complete. Focused audio 52 passed; complete backend 516 passed, 1 skipped, 32 warnings across 5 consecutive runs; compileall succeeded; frontend build compiled. The recorded run returned ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED with ffmpeg_returncode 0, 4000000 microseconds decoded, 17/17 chunks and 0 dropped, sink_mode null and speaker_verified false. Status is READY_FOR_ONE_STORE_LIVE_AUDIO_SOFTWARE_TEST only: NOT_READY_FOR_SPEAKER_TEST and NOT_READY_FOR_PRODUCTION. The protected database was never opened, copied or modified."
  - agent: "main"
    message: "The one-Store Windows output-device pilot is ready for regression validation. The default sink stays null and every automated test uses an injected fake audio backend, so no test can open a real device or play a sound. Hardware mode needs an explicit unambiguous device selector and fails closed otherwise; the Windows default device is never used or changed."
  - agent: "main"
    message: "Windows output-device pilot validation is complete. Focused 40 passed; complete backend 556 passed, 1 skipped, 32 warnings across 5 consecutive runs; compileall succeeded; frontend build compiled; null-sink audio smoke still returns ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED with sink_mode null. sounddevice==0.5.2 was added with operator approval because the installed FFmpeg has no audio output muxer and ffplay cannot target a chosen device. Status is READY_FOR_ONE_STORE_WINDOWS_OUTPUT_TEST only. No hardware test was performed, no operator audible observation was recorded, and SPEAKER_VERIFIED remains unavailable and unclaimed. The protected database was never opened, copied or modified."
  - agent: "main"
    message: "One-Store Windows output hardware validation is complete with outcome HARDWARE_PILOT_BLOCKED: no amplifier and no wired path were available, so no sound was played and no operator audible observation exists. Four real defects were found before any sound and fixed test-first: hardcoded output format, an uncontrolled chime traceback, unstable bare index selectors, and incomplete Bluetooth A2DP detection. Complete backend 581 passed, 1 skipped, 32 warnings across 5 consecutive runs. The protected database was never opened, copied or modified. SPEAKER_VERIFIED remains NOT_IMPLEMENTED."
