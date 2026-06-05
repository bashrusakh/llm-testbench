# Changelog

All notable release changes for LLM Testbench are tracked here.

## v0.1.0

### Removed

- Removed BFCL benchmark support from the backend, UI, fixtures, tests, README, and roadmap.
- Removed BFCL fixture data and the dedicated BFCL adapter module.

### Fixed

- Model selection controls are now locked while a benchmark job is queued, running, or stopping.
- SQL result rendering no longer shows empty tool-call analysis blocks for empty `results_ok` confirmation calls.
- SQL model quantization badges now recognize Unsloth Dynamic GGUF labels such as `UD_IQ1_S` instead of falling back to an unknown format.

### Changed

- Startable benchmark modules are now limited to Speed and SQL Accuracy.
- Fixture validation now covers SQL, Coding Micro, JSON Schema, and Prompt Replay only.
- The README and roadmap now describe the project as a fast, lightweight local benchmark workbench.
- The test badge and development instructions now report 116 passing tests.

### Verification

- Test suite: `116 passed`.
