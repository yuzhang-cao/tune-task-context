#!/usr/bin/env python3
"""Estimate a Codex task's context budget and launch cdx with safe overrides."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 and older
    tomllib = None


MIN_CONTEXT_TOKENS = 32_000
COMPACT_TARGET = 128_000
LARGE_TARGET = 512_000
SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "node_modules", "build", "dist"}
CONTEXT_CONFIG_KEYS = (
    "model_context_window",
    "model_auto_compact_token_limit",
)

HEAVY_MARKERS = (
    "entire repository",
    "whole repository",
    "repository-wide",
    "deep scan",
    "exhaustive",
    "large corpus",
    "long-running",
    "do not stop",
    "整个仓库",
    "全仓库",
    "深度扫描",
    "穷尽",
    "大型语料",
    "长任务",
    "不要停止",
)

LARGE_MARKERS = (
    "multi-file",
    "refactor",
    "migration",
    "manuscript",
    "paper rewrite",
    "old chat",
    "resume",
    "architecture review",
    "多个文件",
    "重构",
    "迁移",
    "论文",
    "文稿",
    "旧聊天",
    "继续之前",
    "架构审查",
)


@dataclass(frozen=True)
class ModelLimits:
    slug: str
    default_window: int
    max_window: int
    effective_percent: int


@dataclass(frozen=True)
class ContextPlan:
    model: str
    task_class: str
    source: str
    requested_tokens: int
    effective_tokens: int
    compact_at_tokens: int
    model_default_tokens: int
    model_max_tokens: int
    effective_percent: int
    repo_files_seen: int
    score: int
    reasons: list[str]
    notes: list[str]
    command: list[str]


def parse_human_tokens(value: str) -> tuple[int, str | None]:
    normalized = value.strip().lower().replace("_", "").replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([km]?)", normalized)
    if not match:
        raise ValueError(f"invalid context size: {value!r}; use values such as 128k, 512k, or 1m")

    amount = float(match.group(1))
    suffix = match.group(2)
    note = None
    if suffix == "m":
        factor = 1_000_000
    elif suffix == "k":
        factor = 1_000
    elif amount < 10_000:
        factor = 1_000
        note = f"interpreted bare value {value!r} as {amount:g}k"
    else:
        factor = 1

    tokens = int(amount * factor)
    if tokens < MIN_CONTEXT_TOKENS:
        raise ValueError(f"context size must be at least {MIN_CONTEXT_TOKENS:,} tokens")
    return tokens, note


def read_top_level_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        if tomllib is not None:
            with path.open("rb") as handle:
                parsed = tomllib.load(handle)
            return {key: parsed[key] for key in (
                "model",
                "model_context_window",
                "model_auto_compact_token_limit",
            ) if key in parsed}
    except (OSError, ValueError):
        return {}

    result: dict[str, Any] = {}
    scalar = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=\s*(.*?)\s*$")
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                break
            match = scalar.match(line)
            if not match:
                continue
            key, raw = match.groups()
            if key not in {"model", "model_context_window", "model_auto_compact_token_limit"}:
                continue
            raw = raw.split("#", 1)[0].strip()
            if raw.startswith(('"', "'")) and raw[-1:] == raw[:1]:
                result[key] = raw[1:-1]
            elif raw.isdigit():
                result[key] = int(raw)
    except OSError:
        return {}
    return result


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def nearest_project_config(cwd: Path) -> Path | None:
    current = cwd.resolve()
    for directory in (current, *current.parents):
        candidate = directory / ".codex" / "config.toml"
        if candidate.is_file():
            return candidate
    return None


def project_root(cwd: Path) -> Path:
    """Return the enclosing Git root, or cwd when no Git worktree is present."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if completed.returncode == 0:
            candidate = Path(completed.stdout.strip()).resolve()
            if candidate.is_dir():
                return candidate
    except (OSError, subprocess.SubprocessError):
        pass
    return cwd.resolve()


def write_context_config(path: Path, window: int, compact_at: int) -> None:
    """Patch only the two top-level context keys and preserve the rest of the TOML file."""
    desired = {
        "model_context_window": window,
        "model_auto_compact_token_limit": compact_at,
    }
    try:
        original = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as exc:
        raise RuntimeError(f"could not read config {path}: {exc}") from exc

    lines = original.splitlines(keepends=True)
    section_index = next(
        (index for index, line in enumerate(lines) if line.lstrip().startswith("[")),
        len(lines),
    )
    key_pattern = re.compile(
        r"^(\s*)(model_context_window|model_auto_compact_token_limit)"
        r"(\s*=\s*)([^#\r\n]*?)(\s*(?:#.*)?)(\r?\n?)$"
    )
    seen: set[str] = set()
    for index in range(section_index):
        match = key_pattern.match(lines[index])
        if not match:
            continue
        indent, key, separator, _, comment, ending = match.groups()
        lines[index] = f"{indent}{key}{separator}{desired[key]}{comment}{ending}"
        seen.add(key)

    missing = [key for key in CONTEXT_CONFIG_KEYS if key not in seen]
    if missing:
        if (
            section_index > 0
            and lines[section_index - 1]
            and not lines[section_index - 1].endswith(("\n", "\r"))
        ):
            lines[section_index - 1] += "\n"
        inserted = [f"{key} = {desired[key]}\n" for key in missing]
        if section_index < len(lines) and (not inserted or inserted[-1].strip()):
            inserted.append("\n")
        lines[section_index:section_index] = inserted

    rendered = "".join(lines)
    if not rendered.endswith("\n"):
        rendered += "\n"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        previous_mode = path.stat().st_mode & 0o777 if path.exists() else None
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        if previous_mode is not None:
            temp_path.chmod(previous_mode)
        os.replace(temp_path, path)
    except OSError as exc:
        try:
            if "temp_path" in locals():
                temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"could not update config {path}: {exc}") from exc


def apply_plan_to_config(plan: ContextPlan, scope: str, cwd: Path) -> Path:
    if scope == "project":
        target = project_root(cwd) / ".codex" / "config.toml"
    elif scope == "user":
        target = codex_home() / "config.toml"
    else:
        raise RuntimeError(f"unsupported config scope: {scope}")
    write_context_config(target, plan.requested_tokens, plan.compact_at_tokens)
    return target


def load_catalog(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"model catalog not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read model catalog {path}: {exc}") from exc
    models = data.get("models")
    if not isinstance(models, list):
        raise RuntimeError(f"model catalog has no models list: {path}")
    return [item for item in models if isinstance(item, dict)]


def select_model_limits(models: list[dict[str, Any]], slug: str) -> ModelLimits:
    for model in models:
        if model.get("slug") != slug:
            continue
        default_window = int(model.get("context_window") or 0)
        max_window = int(model.get("max_context_window") or default_window)
        effective_percent = int(model.get("effective_context_window_percent") or 95)
        if default_window < MIN_CONTEXT_TOKENS or max_window < default_window:
            raise RuntimeError(f"model {slug!r} has invalid context metadata")
        if not 1 <= effective_percent <= 100:
            raise RuntimeError(f"model {slug!r} has invalid effective context percentage")
        return ModelLimits(slug, default_window, max_window, effective_percent)
    raise RuntimeError(f"model {slug!r} is not present in the live catalog")


def count_project_files(cwd: Path, cap: int = 20_001) -> int:
    try:
        probe = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if probe.returncode == 0 and probe.stdout.strip() == "true":
            tracked = subprocess.run(
                ["git", "-C", str(cwd), "ls-files", "-z"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if tracked.returncode == 0:
                return min(tracked.stdout.count(b"\0"), cap)
    except (OSError, subprocess.SubprocessError):
        pass

    seen = 0
    try:
        for _, directories, files in os.walk(cwd):
            directories[:] = [name for name in directories if name not in SKIP_DIRS]
            seen += len(files)
            if seen >= cap:
                return cap
    except OSError:
        return seen
    return seen


def classify_task(prompt: str, repo_files: int, is_resume: bool) -> tuple[str, int, list[str]]:
    lowered = prompt.casefold()
    score = 0
    reasons: list[str] = []
    prompt_length = len(prompt)

    if prompt_length >= 12_000:
        score += 5
        reasons.append("very long initial task description")
    elif prompt_length >= 4_000:
        score += 3
        reasons.append("long initial task description")
    elif prompt_length >= 1_200:
        score += 1
        reasons.append("moderately detailed task description")

    heavy_hits = [marker for marker in HEAVY_MARKERS if marker in lowered]
    if heavy_hits:
        score += 8
        reasons.append(f"maximum-scale marker: {heavy_hits[0]}")

    large_hits = [marker for marker in LARGE_MARKERS if marker in lowered]
    if large_hits:
        score += 2
        reasons.append(f"large-task marker: {large_hits[0]}")

    if repo_files >= 20_000:
        score += 5
        reasons.append("repository has at least 20,000 tracked files")
    elif repo_files >= 5_000:
        score += 3
        reasons.append("repository has at least 5,000 tracked files")
    elif repo_files >= 1_000:
        score += 2
        reasons.append("repository has at least 1,000 tracked files")
    elif repo_files >= 200:
        score += 1
        reasons.append("repository has at least 200 tracked files")

    if is_resume:
        score += 1
        reasons.append("resuming existing history")

    if score <= 1:
        return "compact", score, reasons or ["small prompt and small project"]
    if score <= 4:
        return "standard", score, reasons
    if score <= 7:
        return "large", score, reasons
    return "maximum", score, reasons


def target_for_class(task_class: str, limits: ModelLimits) -> int:
    if task_class == "compact":
        return min(COMPACT_TARGET, limits.default_window)
    if task_class == "standard":
        return limits.default_window
    if task_class == "large":
        return min(max(LARGE_TARGET, limits.default_window), limits.max_window)
    return limits.max_window


def safe_compaction_limit(window: int, effective: int) -> int:
    return min(math.floor(window * 0.90), math.floor(effective * 0.95))


def resolve_cdx_binary(explicit: str | None, dry_run: bool) -> str:
    if explicit:
        return explicit
    configured = os.environ.get("CDX_BIN")
    if configured:
        return configured

    candidates = [shutil.which("cdx"), shutil.which("codex")]
    app_binary = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    if app_binary.is_file():
        candidates.append(str(app_binary))
    unique_candidates = list(dict.fromkeys(candidate for candidate in candidates if candidate))

    versioned: list[tuple[tuple[int, int, int], str]] = []
    for candidate in unique_candidates:
        try:
            completed = subprocess.run(
                [candidate, "--version"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", completed.stdout + completed.stderr)
        if completed.returncode == 0 and match:
            versioned.append((tuple(int(part) for part in match.groups()), candidate))
    if versioned:
        return max(versioned)[1]
    if unique_candidates:
        return unique_candidates[0]
    if dry_run:
        return "cdx"
    raise RuntimeError("could not find cdx; set CDX_BIN or pass --cdx-bin")


def build_plan(args: argparse.Namespace) -> ContextPlan:
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise RuntimeError(f"working directory does not exist: {cwd}")

    home = codex_home()
    user_config = read_top_level_config(home / "config.toml")
    project_path = nearest_project_config(cwd)
    project_config = read_top_level_config(project_path) if project_path else {}

    catalog_path = Path(args.models_json).expanduser() if args.models_json else home / "models_cache.json"
    models = load_catalog(catalog_path)
    model_slug = args.model or project_config.get("model") or user_config.get("model")
    if not model_slug:
        visible = [item.get("slug") for item in models if item.get("visibility") != "hide"]
        model_slug = next((slug for slug in visible if isinstance(slug, str)), None)
    if not isinstance(model_slug, str):
        raise RuntimeError("could not resolve a model; pass --model")
    limits = select_model_limits(models, model_slug)

    prompt = " ".join(args.prompt).strip()
    repo_files = count_project_files(cwd)
    task_class, score, reasons = classify_task(prompt, repo_files, bool(args.resume))
    notes: list[str] = []

    force_auto = args.context is not None and args.context.casefold() == "auto"
    if args.context and not force_auto:
        if args.context.casefold() == "max":
            requested = limits.max_window
            source = "manual:max"
        else:
            requested, interpretation = parse_human_tokens(args.context)
            source = "manual"
            if interpretation:
                notes.append(interpretation)
    elif not force_auto and isinstance(project_config.get("model_context_window"), int):
        requested = int(project_config["model_context_window"])
        source = f"project:{project_path}"
    else:
        requested = target_for_class(task_class, limits)
        source = f"auto:{task_class}"

    if requested > limits.max_window:
        notes.append(f"clamped requested {requested:,} to model maximum {limits.max_window:,}")
        requested = limits.max_window
    if requested < MIN_CONTEXT_TOKENS:
        raise RuntimeError(f"resolved context is below {MIN_CONTEXT_TOKENS:,} tokens")

    effective = math.floor(requested * limits.effective_percent / 100)
    compact_safe = safe_compaction_limit(requested, effective)
    compact_source: str | None = None
    if args.compact_at:
        compact_requested, interpretation = parse_human_tokens(args.compact_at)
        compact_source = "manual"
        if interpretation:
            notes.append(interpretation)
    elif source.startswith("project:") and isinstance(project_config.get("model_auto_compact_token_limit"), int):
        compact_requested = int(project_config["model_auto_compact_token_limit"])
        compact_source = "project"
    else:
        compact_requested = compact_safe

    compact_at = min(compact_requested, compact_safe)
    if compact_requested != compact_at:
        notes.append(
            f"clamped {compact_source or 'requested'} compaction threshold "
            f"{compact_requested:,} to safe limit {compact_at:,}"
        )

    cdx_binary = resolve_cdx_binary(
        args.cdx_bin,
        args.dry_run or bool(args.apply_config),
    )
    config_args = [
        "-m",
        limits.slug,
        "-c",
        f"model_context_window={requested}",
        "-c",
        f"model_auto_compact_token_limit={compact_at}",
        "-C",
        str(cwd),
    ]
    if args.resume:
        command = [cdx_binary, "resume", *config_args]
        if args.resume.casefold() == "last":
            command.append("--last")
        else:
            command.append(args.resume)
    else:
        command = [cdx_binary, *config_args]
    if prompt:
        command.append(prompt)

    return ContextPlan(
        model=limits.slug,
        task_class=task_class,
        source=source,
        requested_tokens=requested,
        effective_tokens=effective,
        compact_at_tokens=compact_at,
        model_default_tokens=limits.default_window,
        model_max_tokens=limits.max_window,
        effective_percent=limits.effective_percent,
        repo_files_seen=repo_files,
        score=score,
        reasons=reasons,
        notes=notes,
        command=command,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Estimate a task context window and launch cdx with safe per-task overrides."
    )
    result.add_argument("--context", help="auto, max, or a size such as 128k, 512k, or 1m")
    result.add_argument("--compact-at", help="optional manual compaction threshold")
    result.add_argument("--model", help="model slug; defaults to project or user config")
    result.add_argument("--resume", metavar="SESSION_ID", help="resume a session id; use 'last' for --last")
    result.add_argument("--cwd", default=os.getcwd(), help="working directory to inspect and pass to cdx")
    result.add_argument("--models-json", help="path to a Codex models catalog JSON file")
    result.add_argument("--cdx-bin", help="path or command name for cdx")
    output_mode = result.add_mutually_exclusive_group()
    output_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without launching cdx",
    )
    output_mode.add_argument(
        "--apply-config",
        choices=("project", "user"),
        help="write the selected window for future tasks instead of launching cdx",
    )
    result.add_argument("prompt", nargs=argparse.REMAINDER, help="initial task prompt after --")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.prompt and args.prompt[0] == "--":
        args.prompt = args.prompt[1:]
    try:
        plan = build_plan(args)
    except (RuntimeError, ValueError) as exc:
        print(f"context preflight failed: {exc}", file=sys.stderr)
        return 2

    if args.apply_config:
        try:
            target = apply_plan_to_config(
                plan,
                args.apply_config,
                Path(args.cwd).expanduser().resolve(),
            )
        except RuntimeError as exc:
            print(f"context apply failed: {exc}", file=sys.stderr)
            return 2
        payload = asdict(plan)
        payload.update(
            {
                "applied_to": str(target),
                "applied_scope": args.apply_config,
                "applies_to": "future_new_or_resumed_tasks",
                "command_display": shlex.join(plan.command),
            }
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.dry_run:
        payload = asdict(plan)
        payload["command_display"] = shlex.join(plan.command)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(
        f"Context: requested {plan.requested_tokens:,} | effective {plan.effective_tokens:,} | "
        f"compact at {plan.compact_at_tokens:,} | source {plan.source}",
        file=sys.stderr,
    )
    for note in plan.notes:
        print(f"Context note: {note}", file=sys.stderr)
    os.execvp(plan.command[0], plan.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
