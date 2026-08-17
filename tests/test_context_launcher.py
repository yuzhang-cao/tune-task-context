from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "context_launcher.py"


def load_launcher_module():
    spec = importlib.util.spec_from_file_location("context_launcher", LAUNCHER)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load context_launcher")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_models_cache(codex_home: Path) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-test",
                        "visibility": "list",
                        "context_window": 272_000,
                        "max_context_window": 872_000,
                        "effective_context_window_percent": 95,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text('model = "gpt-test"\n', encoding="utf-8")


class ContextLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.launcher = load_launcher_module()

    def test_parse_human_tokens_supports_ui_values(self) -> None:
        self.assertEqual(self.launcher.parse_human_tokens("625k")[0], 625_000)
        self.assertEqual(self.launcher.parse_human_tokens("1m")[0], 1_000_000)
        self.assertEqual(self.launcher.parse_human_tokens("512")[0], 512_000)

    def test_dry_run_and_apply_are_mutually_exclusive(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.launcher.parser().parse_args(
                    ["--dry-run", "--apply-config", "project"]
                )

    def test_write_context_config_preserves_comments_and_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text(
                'model = "gpt-test"\n'
                "model_context_window = 128000 # keep this note\n\n"
                "[features]\n"
                "web_search = true\n",
                encoding="utf-8",
            )

            self.launcher.write_context_config(config, 625_000, 562_500)

            rendered = config.read_text(encoding="utf-8")
            self.assertIn("model_context_window = 625000 # keep this note", rendered)
            self.assertIn("model_auto_compact_token_limit = 562500", rendered)
            self.assertIn("[features]\nweb_search = true", rendered)

    def test_apply_project_config_returns_future_task_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            project = root / "project"
            project.mkdir()
            write_models_cache(codex_home)
            env = {**os.environ, "CODEX_HOME": str(codex_home)}
            completed = subprocess.run(
                [
                    sys.executable,
                    str(LAUNCHER),
                    "--cwd",
                    str(project),
                    "--context",
                    "625k",
                    "--apply-config",
                    "project",
                    "--",
                    "Refactor several modules",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["requested_tokens"], 625_000)
            self.assertEqual(payload["applies_to"], "future_new_or_resumed_tasks")
            config = project / ".codex" / "config.toml"
            self.assertTrue(config.is_file())
            self.assertIn("model_context_window = 625000", config.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
