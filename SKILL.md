---
name: tune-task-context
description: Give each Codex task the amount of context it actually needs. Use when someone opens the context control, wants Codex to analyze a project before work starts, selects Auto or Details, moves a 256K-to-1M context slider, enters an exact token limit, changes compaction, enlarges an old task, or applies task-, project-, profile-, or user-level model_context_window settings. Inspect the live model ceiling, let explicit user choices win, apply the narrowest safe override before a new or resumed task, and verify the effective runtime window.
---

# Tune Task Context

Choose a context budget before work starts, then launch cdx with that budget. Prefer the interactive **上下文** control when its MCP tools are available; keep the CLI launcher as the complete fallback. Keep the decision visible: report the requested window, effective window, compaction point, source, and any model-side clamp.

## Open the context control

When the user asks to open, show, inspect, or adjust context and the plugin tools are available:

1. Resolve the absolute current project path without reading file contents.
2. Call `show_context_control` with the project path, next-task description, and resume state; it performs the initial analysis and renders the **上下文** card once.
3. Use `analyze_context` for a headless estimate or when the card requests **重新分析**.
4. Treat **自动** as an estimate from task-scale signals. Treat **详情** as an explicit override; the slider presets are 256K, 512K, 625K, 750K, and 1M. The exact field accepts an integer K value from 32 to 1,000 and automatically appends the final three zeroes.
5. Call `apply_context` only after the user presses **应用** or explicitly confirms a value. Default to project scope. State that it affects the next new or resumed task, never the task already running.

Keep all three tools useful without rendered UI. If the tools are unavailable, use `scripts/context_launcher.py` directly.

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

To persist a confirmed selection for the next task in the current project without launching a second process:

```bash
python3 scripts/context_launcher.py --cwd <project> --context 625k --apply-config project -- "Next task"
```

This patches only `model_context_window` and `model_auto_compact_token_limit` at the top level of `.codex/config.toml`.

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
- Never call `apply_context` without an explicit confirmation signal.
