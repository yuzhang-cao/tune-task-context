---
name: tune-task-context
description: Give each Codex task the amount of context it actually needs. Use when someone wants Codex to estimate a context window before starting work, set a manual limit such as 128k or 512k, enlarge an old task, keep a small task lightweight, tune compaction, or apply task-, project-, profile-, or user-level model_context_window settings. Inspect the live model ceiling, let explicit user choices win, launch or resume cdx with the narrowest safe override, and verify the effective runtime window.
---

# Tune Task Context

Choose a context budget before work starts, then launch Codex with that budget. Keep the decision visible: report the requested window, effective window, compaction point, source, and any model-side clamp.

## Respect the lifecycle boundary

- Apply a new window before creating or resuming a task. A skill loaded inside an already-running first turn cannot resize that same turn.
- Treat `UserPromptSubmit` hooks as advisory. They may add context or block a prompt, but they do not expose a supported context-window mutation.
- Do not claim that editing `config.toml` resized the currently active task. State that the change applies to a new or explicitly resumed process unless current runtime evidence proves otherwise.
- Do not edit `state_*.sqlite`, rollout JSONL, or other internal task records to force a switch.

## Preflight workflow

1. Capture the initial task description before launching the working task.
2. Look for an explicit user choice such as `128k`, `512k`, `1m`, or `max`. Let it override every estimate.
3. Inspect the live model catalog instead of hardcoding a model ceiling. Prefer `$CODEX_HOME/models_cache.json`, falling back to `~/.codex/models_cache.json` or `cdx debug models` when needed.
4. Inspect only task-scale signals: prompt length, high-level task markers, tracked-file count, whether the task resumes old history, and existing top-level context keys. Do not read repository contents merely to size the window.
5. Run `scripts/context_launcher.py --dry-run -- <task>` to calculate the plan. Review its clamp notes and command before launching.
6. Launch automatically when the user requested execution. Keep shell arguments as an array; never construct an eval string.
7. Verify the new task after its first response. Prefer the task's `token_count.model_context_window` or a redacted runtime log entry. Distinguish the configured request from the effective window.

## Selection policy

Use these as starting points, bounded by the selected model's live maximum:

| Class | Typical work | Requested window |
| --- | --- | ---: |
| Compact | One-file fixes, short answers, tiny scripts | up to 128k |
| Standard | Normal feature work, debugging, focused review | model default |
| Large | Multi-file refactors, migrations, manuscripts, long resumptions | at least 512k |
| Maximum | Repository-wide deep scans, very large corpora, long autonomous work | live model maximum |

Prefer a smaller class when evidence is mixed. A larger window carries more history but does not improve every task and may increase latency or usage.

## Manual overrides

Accept human-sized values. Interpret bare values below 10,000 as thousands, so `512` means `512k`; prefer an explicit suffix in generated commands.

```bash
python3 scripts/context_launcher.py --context 512k --dry-run -- "Refactor the networking layer"
python3 scripts/context_launcher.py --context max --dry-run -- "Audit the entire repository"
python3 scripts/context_launcher.py --context auto --dry-run -- "Fix the typo in one file"
```

Use `--compact-at` only when the user explicitly asks for a separate threshold. Clamp it below both the requested window and the model's effective safety limit.

## Apply the narrowest scope

### One task

Use the launcher. It injects `model_context_window` and `model_auto_compact_token_limit` through cdx `-c` overrides before the first model request.

```bash
python3 scripts/context_launcher.py --context auto -- "Implement the requested change"
```

### One old task

Resume it with an explicit model and the selected context. Existing compaction is not reversible.

```bash
python3 scripts/context_launcher.py --resume <SESSION_ID> --context 512k -- "Continue the task"
```

### One trusted project

Merge only these top-level keys into `<project>/.codex/config.toml` after inspecting the existing file:

```toml
model_context_window = 512000
model_auto_compact_token_limit = 460800
```

Preserve comments and unrelated settings. Project config loads only for a trusted project.

### Reusable named policy

Create `$CODEX_HOME/<name>.config.toml` and select it with `cdx --profile <name>`. Use profiles for stable policies such as `compact`, `writing`, or `deep-audit`.

### User default

Edit `$CODEX_HOME/config.toml` only when the user explicitly asks to change the default for future tasks. Read first, patch only the relevant keys, and verify parsing afterward.

## Report the result

Use one concise status line:

```text
Context: requested 512,000 | effective 486,400 | compact at 460,800 | source manual
```

If clamped, say so directly. Never describe a marketed total context figure as literal usable history when the runtime reports a smaller effective input window.

## Safety rules

- Never print tokens, credentials, provider headers, or unrelated config values.
- Never overwrite a whole config file to change two keys.
- Never assume a model's limit from its name; catalogs and account availability can change.
- Never silently replace an explicit user value with an estimate.
- Never launch a second billable task during `--dry-run` or validation.
