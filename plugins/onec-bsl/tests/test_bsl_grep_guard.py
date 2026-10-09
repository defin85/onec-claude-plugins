import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
HOOK = PLUGIN / "hooks" / "bsl_grep_guard.py"


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "project"
        cfg = self.project / "src" / "cf" / "demo"
        (cfg / "CommonModules" / "Общий" / "Ext").mkdir(parents=True)
        (cfg / "Configuration.xml").write_text("<MetaDataObject/>", encoding="utf-8")
        self.module = cfg / "CommonModules" / "Общий" / "Ext" / "Module.bsl"
        self.module.write_text("Процедура Тест() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
        (self.project / "src" / "cfe" / "Ext1").mkdir(parents=True)
        (self.project / "docs").mkdir()
        (self.project / "docs" / "guide.md").write_text("text", encoding="utf-8")
        # выгрузка без Configuration.xml вне src/: по умолчанию не защищена, только через sourceRoots
        (self.project / "vendor" / "my-config" / "CommonModules").mkdir(parents=True)
        (self.project / ".git").mkdir()
        # отдельная выгрузка вне стандартных каталогов: распознаётся по Configuration.xml
        self.loose = self.project / "dumps" / "custom"
        (self.loose / "Documents").mkdir(parents=True)
        (self.loose / "Configuration.xml").write_text("<MetaDataObject/>", encoding="utf-8")
        self.plain = Path(self.tmp.name) / "plain"
        (self.plain / "lib").mkdir(parents=True)
        (self.plain / ".git").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, payload, project=None, raw=None):
        env = dict(os.environ)
        env["CLAUDE_PROJECT_DIR"] = str(project or self.project)
        data = raw if raw is not None else json.dumps(payload)
        proc = subprocess.run(
            [sys.executable, str(HOOK)], input=data, capture_output=True, text=True, env=env, timeout=10
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def bash(self, command, cwd=None, project=None):
        return self.run_hook(
            {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(cwd or self.project),
             "tool_input": {"command": command}},
            project=project,
        )

    def grep_tool(self, path=None, cwd=None, **extra):
        tool_input = {"pattern": "Тест"}
        if path is not None:
            tool_input["path"] = str(path)
        tool_input.update(extra)
        return self.run_hook(
            {"hook_event_name": "PreToolUse", "tool_name": "Grep", "cwd": str(cwd or self.project),
             "tool_input": tool_input}
        )

    def assertDenied(self, result):
        self.assertIsNotNone(result, "ожидался запрет")
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("symbol_info", out["permissionDecisionReason"])
        self.assertIn("BSL_GREP_REASON", out["permissionDecisionReason"])

    # --- блокировка

    def test_blocks_rg_in_src_cf(self):
        self.assertDenied(self.bash("rg -n 'Тест' src/cf/demo"))
        self.assertDenied(self.bash("rg Тест src/cf"))

    def test_blocks_rg_with_cwd_inside_dump(self):
        self.assertDenied(self.bash("rg Тест", cwd=self.project / "src" / "cf" / "demo"))
        self.assertDenied(self.bash("cd src/cf/demo && rg Тест"))

    def test_blocks_grep_variants(self):
        self.assertDenied(self.bash("grep -rn Тест src/cf"))
        self.assertDenied(self.bash("grep -R --include='*.bsl' Тест src/cfe"))
        self.assertDenied(self.bash("ugrep Тест src/cf/demo"))
        self.assertDenied(self.bash("git grep -n Тест -- src/cf"))
        self.assertDenied(self.bash("find src/cf -name '*.bsl' -exec grep -l Тест {} +"))
        self.assertDenied(self.bash("find src/cf -name '*.bsl' | xargs grep -l Тест"))
        self.assertDenied(self.bash("grep Тест src/cf/demo/CommonModules/*/Ext/Module.bsl"))

    def test_blocks_configuration_xml_dump(self):
        self.assertDenied(self.bash("rg Тест dumps/custom/Documents"))

    def test_defaults_are_only_generic_roots(self):
        self.assertIsNone(self.bash("rg Тест vendor/my-config"))

    def test_project_config_glob_source_roots(self):
        (self.project / ".claude").mkdir()
        (self.project / ".claude" / "onec-bsl.json").write_text(
            json.dumps({"sourceRoots": ["**/my-config"]}), encoding="utf-8"
        )
        self.assertDenied(self.bash("rg Тест vendor/my-config"))
        self.assertDenied(self.bash("rg Тест vendor/my-config/CommonModules"))

    def test_blocks_search_covering_dumps_from_root(self):
        self.assertDenied(self.bash("rg Тест"))
        self.assertDenied(self.bash("grep -rn Тест ."))
        self.assertDenied(self.bash("rg Тест src"))

    def test_blocks_search_covering_worktree_dumps(self):
        (self.project / ".worktrees" / "wt1" / "src" / "cf").mkdir(parents=True)
        (self.project / ".worktrees" / "wt1" / ".git").write_text("gitdir: ../../.git/worktrees/wt1\n")
        self.assertDenied(self.bash("rg Тест .worktrees"))
        self.assertDenied(self.bash("rg Тест .worktrees/wt1/src/cf"))

    def test_blocks_grep_tool_in_dump(self):
        self.assertDenied(self.grep_tool(self.project / "src" / "cf" / "demo"))
        self.assertDenied(self.grep_tool("src/cfe/Ext1"))
        self.assertDenied(self.grep_tool(None, cwd=self.project / "src" / "cf"))

    def test_blocks_unlimited_name_listing(self):
        self.assertDenied(self.bash("rg --files src/cf"))
        self.assertDenied(self.bash("find src/cf -name '*.bsl'"))

    def test_command_substitution_and_redirections(self):
        self.assertDenied(self.bash("echo $(rg -l Тест src/cf) 2>&1"))
        self.assertDenied(self.bash("rg Тест src/cf 2>/dev/null | head -5"))
        self.assertDenied(self.bash("bash -c 'rg Тест src/cf'"))

    # --- разрешения

    def test_allows_single_file(self):
        self.assertIsNone(self.bash(f"rg -n Тест {self.module}"))
        self.assertIsNone(self.bash(f"grep -n Тест '{self.module}'"))
        self.assertIsNone(self.grep_tool(self.module))

    def test_allows_reason_prefix(self):
        self.assertIsNone(self.bash("BSL_GREP_REASON='worktree без индекса' rg Тест src/cf"))
        self.assertIsNone(self.bash("env BSL_GREP_REASON=проверка grep -rn Тест src/cf"))
        self.assertIsNone(self.bash("export BSL_GREP_REASON=x; rg Тест src/cf"))
        self.assertDenied(self.bash("BSL_GREP_REASON= rg Тест src/cf"))
        self.assertDenied(self.bash("BSL_GREP_REASON='' rg Тест src/cf"))

    def test_allows_paths_outside_dumps(self):
        self.assertIsNone(self.bash("rg -n Тест docs scripts"))
        self.assertIsNone(self.bash("grep -rn Тест docs"))
        self.assertIsNone(self.grep_tool(self.project / "docs"))
        self.assertIsNone(self.bash("rg Тест -g '!src/**'"))
        self.assertIsNone(self.bash("rg -t py Тест"))
        self.assertIsNone(self.bash("cat x | grep Тест"))
        self.assertIsNone(self.bash("git log --oneline | rg fix"))
        self.assertIsNone(self.bash("ls src/cf"))

    def test_allows_limited_name_listing(self):
        self.assertIsNone(self.bash("rg --files src/cf | head -50"))
        self.assertIsNone(self.bash("rg --files src/cf | rg Module.bsl | head -20"))
        self.assertIsNone(self.bash("find src/cf -maxdepth 2 -type d"))
        self.assertIsNone(self.bash("find src/cf -name '*.bsl' | wc -l"))

    def test_allows_when_no_dumps(self):
        self.assertIsNone(self.bash("rg -n foo", cwd=self.plain, project=self.plain))
        self.assertIsNone(self.bash("grep -rn foo lib", cwd=self.plain, project=self.plain))
        self.assertIsNone(self.bash("find . -name '*.py'", cwd=self.plain, project=self.plain))

    def test_project_config_adds_source_roots(self):
        extra = self.project / "vendor" / "conf"
        extra.mkdir(parents=True)
        self.assertIsNone(self.bash("rg Тест vendor/conf"))
        (self.project / ".claude").mkdir()
        (self.project / ".claude" / "onec-bsl.json").write_text(
            json.dumps({"sourceRoots": ["vendor/conf"]}), encoding="utf-8"
        )
        self.assertDenied(self.bash("rg Тест vendor/conf"))
        self.assertDenied(self.bash("rg Тест vendor"))

    # --- устойчивость

    def test_fail_open_on_broken_json(self):
        result = self.run_hook(None, raw="{not json")
        self.assertIn("systemMessage", result)
        self.assertNotIn("hookSpecificOutput", result)

    def test_fail_open_on_broken_project_config(self):
        (self.project / ".claude").mkdir()
        (self.project / ".claude" / "onec-bsl.json").write_text("{", encoding="utf-8")
        result = self.bash("rg Тест src/cf")
        self.assertIn("systemMessage", result)

    def test_unbalanced_quotes_fail_open(self):
        result = self.bash("rg 'Тест src/cf")
        self.assertIn("systemMessage", result)

    def test_other_tools_ignored(self):
        self.assertIsNone(self.run_hook({"tool_name": "Read", "cwd": str(self.project),
                                         "tool_input": {"file_path": str(self.module)}}))

    def test_fast(self):
        started = time.monotonic()
        for _ in range(5):
            self.bash("rg -n Тест src/cf/demo")
        self.assertLess((time.monotonic() - started) / 5, 0.5)


if __name__ == "__main__":
    unittest.main()
