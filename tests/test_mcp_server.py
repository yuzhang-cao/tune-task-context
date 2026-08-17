from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_context_launcher import ROOT, write_models_cache


SERVER = ROOT / "dist" / "server.mjs"


class McpServerTests(unittest.TestCase):
    def test_tools_resource_and_confirmed_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp_root = Path(directory)
            codex_home = temp_root / "codex-home"
            project = temp_root / "project"
            project.mkdir()
            write_models_cache(codex_home)
            env = {**os.environ, "CODEX_HOME": str(codex_home)}
            messages = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "tune-task-context-test", "version": "1.0.0"},
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "analyze_context",
                        "arguments": {
                            "project_path": str(project),
                            "task_description": "Refactor multiple files",
                            "is_resume": False,
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "resources/read",
                    "params": {"uri": "ui://tune-task-context/context-control-v1.html"},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "apply_context",
                        "arguments": {
                            "project_path": str(project),
                            "task_description": "Refactor multiple files",
                            "is_resume": False,
                            "mode": "manual",
                            "context_tokens": 100000,
                            "scope": "project",
                            "confirmed": True,
                        },
                    },
                },
            ]
            input_text = "".join(json.dumps(message) + "\n" for message in messages)
            completed = subprocess.run(
                ["node", str(SERVER)],
                input=input_text,
                capture_output=True,
                text=True,
                cwd=ROOT,
                env=env,
                timeout=25,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            responses = {
                item["id"]: item
                for line in completed.stdout.splitlines()
                if line.strip()
                for item in [json.loads(line)]
                if "id" in item
            }
            self.assertEqual(responses[1]["result"]["serverInfo"]["name"], "tune-task-context")
            names = {tool["name"] for tool in responses[2]["result"]["tools"]}
            self.assertEqual(names, {"analyze_context", "show_context_control", "apply_context"})
            self.assertEqual(
                responses[3]["result"]["structuredContent"]["project_path"],
                str(project),
            )
            resource = responses[4]["result"]["contents"][0]
            self.assertEqual(resource["mimeType"], "text/html;profile=mcp-app")
            self.assertIn("256K", resource["text"])
            self.assertEqual(responses[5]["result"]["structuredContent"]["requested_tokens"], 100000)
            self.assertIn(
                "model_context_window = 100000",
                (project / ".codex" / "config.toml").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
