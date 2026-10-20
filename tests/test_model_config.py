"""Offline tests of the standalone Bash script's config and CLI boundaries."""
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "openclaw.sh").read_text()
FLASH = "MiniMax-M3.1-Flash"
M3 = "MiniMax-M3"
LEGACY = "MiniMax-M2.7"
REMOTE = [LEGACY, "MiniMax-M2.5", "custom-model", M3, FLASH, "speech-2.8-hd"]


def function(name):
    return re.search(r"\t" + re.escape(name) + r"\(\) \{\n.*?^\t\}\n(?=\n)", SOURCE, re.M | re.S).group()


class ModelConfigTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(dir=os.environ.get("TEST_WORKDIR", ROOT / "tests"))
        self.addCleanup(self.work.cleanup)
        self.home = Path(self.work.name)
        (self.home / ".openclaw").mkdir()
        self.config = self.home / ".openclaw/openclaw.json"
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "LANG": "C.UTF-8"}
        self.config.write_text("{}")
        self.remote = self.home / "remote.json"
        self.remote.write_text(json.dumps({"data": [{"id": mid} for mid in REMOTE]}))

    def run_shell(self, code, input=None):
        result = subprocess.run(["bash", "-c", code], env=self.env, input=input,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def import_models(self, url):
        code = function("minimax_model_choices") + "\n" + function("add-all-models-from-provider")
        # Capture the actual curl argument boundary without opening a network socket.
        code += '''
        gum() { while [[ "$1" != -- ]]; do shift; done; shift; "$@"; }
        curl() {
            [[ "$*" == *"Authorization: Bearer fixture-key"* ]] || return 8
            [[ "${@: -1}" == "$BASE/models" ]] || return 9
            while [[ "$1" != -o ]]; do shift; done
            cp "$HOME/remote.json" "$2"
        }
        add-all-models-from-provider direct "$BASE" fixture-key
        '''
        self.env["BASE"] = url
        self.run_shell(code)
        return json.loads(self.config.read_text())

    def test_import_regions_and_gateway_metadata(self):
        for url in ("https://api.minimax.io/v1", "https://api.minimaxi.com/v1", "https://gateway.example/v1"):
            with self.subTest(url=url):
                self.config.write_text("{}")
                result = self.import_models(url)
                provider = result["models"]["providers"]["direct"]
                self.assertEqual(provider["api"], "openai-completions")
                self.assertEqual(provider["baseUrl"], url)
                ids = [m["id"] for m in provider["models"]]
                self.assertEqual(ids, [FLASH, "custom-model", M3, "speech-2.8-hd"])
                flash = provider["models"][0]
                if "gateway" not in url:
                    self.assertEqual(flash["contextWindow"], 1000000)
                    self.assertEqual(flash["maxTokens"], 524288)
                    self.assertEqual(flash["input"], ["text", "image", "video"])
                    self.assertEqual(flash["cost"], {"input": .15, "output": .6, "cacheRead": .015, "cacheWrite": None})
                    m3 = provider["models"][2]
                    self.assertEqual(m3["cost"]["input"], .3)
                else:
                    self.assertEqual(flash["contextWindow"], 1048576)
                self.assertNotIn("model", result["agents"]["defaults"])

    def test_import_preserves_saved_custom_legacy_and_overrides(self):
        saved = [{"id": LEGACY, "maxTokens": 42, "input": ["text"]},
                 {"id": "private-alias", "maxTokens": 99}, {"id": FLASH, "maxTokens": 123}]
        defaults = {"model": {"primary": "direct/" + LEGACY},
                    "models": {"direct/" + LEGACY: {"params": {"temperature": .7}}}}
        self.config.write_text(json.dumps({"models": {"providers": {"direct": {"models": saved}}},
                                         "agents": {"defaults": defaults}}))
        result = self.import_models("https://api.minimax.io/v1")
        models = {m["id"]: m for m in result["models"]["providers"]["direct"]["models"]}
        for model in saved:
            self.assertEqual(models[model["id"]], model)
        self.assertEqual(result["agents"]["defaults"]["model"], defaults["model"])
        self.assertEqual(result["agents"]["defaults"]["models"]["direct/" + LEGACY],
                         defaults["models"]["direct/" + LEGACY])

    def test_gateway_keeps_saved_custom_aliases(self):
        saved = [{"id": "private-alias", "maxTokens": 99}, {"id": LEGACY, "maxTokens": 42}]
        self.config.write_text(json.dumps({"models": {"providers": {"direct": {"models": saved}}}}))
        result = self.import_models("https://gateway.example/v1")
        models = {m["id"]: m for m in result["models"]["providers"]["direct"]["models"]}
        for model in saved:
            self.assertEqual(models[model["id"]], model)

    def test_picker_cli_boundary(self):
        code = function("minimax_model_choices") + "\n" + function("change_model") + '''
        openclaw() {
            if [[ "$1 $2" == "models list" ]]; then
                printf '%s configured\\n' direct/MiniMax-M2.7 direct/MiniMax-M3 direct/MiniMax-M3.1-Flash other/custom
            else
                printf '%s\\n' "$@" > "$HOME/selected"
            fi
        }
        fzf() { head -1; }
        start_gateway() { :; }
        break_end() { :; }
        gum() { while [[ "$1" != -- ]]; do shift; done; shift; "$@"; }
        ui_ok() { :; }
        change_model
        '''
        self.run_shell(code)
        self.assertEqual((self.home / "selected").read_text().splitlines(), ["models", "set", "direct/" + FLASH])

    def test_new_provider_default_cli_boundary(self):
        code = "\n".join(function(name) for name in
                         ("minimax_model_choices", "add-all-models-from-provider", "add-openclaw-provider-interactive"))
        code += '''
        gum() {
            case "$1" in
                input)
                    case "$*" in
                        *password*) echo fixture-key ;;
                        *Provider*) echo direct ;;
                        *) echo https://api.minimax.io/v1 ;;
                    esac ;;
                spin) while [[ "$1" != -- ]]; do shift; done; shift; "$@" ;;
                *) return 0 ;;
            esac
        }
        curl() { while [[ "$1" != -o ]]; do shift; done; cp "$HOME/remote.json" "$2"; }
        fzf() { return 1; }
        install() { :; }
        openclaw() { printf '%s\\n' "$@" > "$HOME/selected"; }
        start_gateway() { :; }
        break_end() { :; }
        add-openclaw-provider-interactive
        '''
        self.run_shell(code)
        self.assertEqual((self.home / "selected").read_text().splitlines(), ["models", "set", "direct/" + FLASH])

    def test_gateway_routes_and_nontext_choices(self):
        ids = ["minimax/minimax-m2.7", "minimax/minimax-m3", "minimax/minimax-m3.1-flash:free",
               "minimax-m3.1-flash-free", "speech-2.8-hd", "custom-model"]
        result = self.run_shell(function("minimax_model_choices") + "\nminimax_model_choices", "\n".join(ids))
        self.assertEqual(result.splitlines(), [ids[2], ids[3], ids[1], ids[4], ids[5]])

    def test_sync_preserves_saved_models_and_applies_new_metadata(self):
        provider = {"baseUrl": "https://api.minimax.io/v1", "apiKey": "fixture-key",
                    "api": "openai-completions", "models": [{"id": LEGACY, "maxTokens": 42}, {"id": "private-alias"}]}
        defaults = {"model": {"primary": "direct/" + LEGACY}, "models": {"direct/" + LEGACY: {}}}
        self.config.write_text(json.dumps({"models": {"providers": {"direct": provider}}, "agents": {"defaults": defaults}}))
        code = function("sync_openclaw_api_models").split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        requests = []
        def transport(request, **kwargs):
            requests.append(request)
            return io.BytesIO(self.remote.read_bytes())
        with patch.object(sys, "argv", ["sync", str(self.config)]), patch("urllib.request.urlopen", transport):
            exec(compile(code, "openclaw.sh:sync", "exec"), {})
        self.assertEqual(requests[0].full_url, provider["baseUrl"] + "/models")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer fixture-key")
        result = json.loads(self.config.read_text())
        models = {m["id"]: m for m in result["models"]["providers"]["direct"]["models"]}
        self.assertEqual(models[LEGACY], provider["models"][0])
        self.assertIn("private-alias", models)
        self.assertEqual(models[FLASH]["maxTokens"], 524288)
        self.assertEqual(result["agents"]["defaults"]["model"], defaults["model"])
        self.assertNotIn("MiniMax-M2.5", models)


if __name__ == "__main__":
    unittest.main()
