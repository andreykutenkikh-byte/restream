"""Pin the HUD release's unchanged main media/control boundary and CI gates."""

import ast
import hashlib
import io
import json
import tokenize
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/hud_release"
MAIN_SHA = "8136480d22ca7d4fdae82c70b4b34c1d638a9026"
MAIN_CI_BLOB = "adbf87e76d14c85eebbcc3683411551521c52d73"
PROTECTED_TREES = ("bootstrap_worker", "node_agent", "relay_agent", "deploy", "mediamtx")
PREVIEW_FORMAT_EXCEPTION = "relay_agent/preview.py"
# The subsequent server-recovery release explicitly changes these three files.
# Preserve the historical HUD manifest and pin this narrow delta separately.
INSTALLER_REPAIR_FILES = {
    "bootstrap_worker/installer.py",
    "bootstrap_worker/errors.py",
    "bootstrap_worker/ssh.py",
}
# The owner requested safe co-installation on existing Docker/Amnezia hosts.
# Keep the earlier release manifests intact and pin only this authorized delta.
COEXISTENCE_FILES = {
    "app/services/bootstrap.py",
    "bootstrap_worker/compose_plugin.py",
    "bootstrap_worker/errors.py",
    "bootstrap_worker/installer.py",
    "bootstrap_worker/jobs.py",
    "bootstrap_worker/models.py",
    "bootstrap_worker/state_machine.py",
}


def _git_blob(path: Path) -> str:
    # .gitattributes requires LF for these text files; tolerate checkout CRLF only.
    content = path.read_bytes().replace(b"\r\n", b"\n")
    blob = b"blob " + str(len(content)).encode("ascii") + b"\0" + content
    return hashlib.sha1(blob, usedforsecurity=False).hexdigest()


def _code_tokens(source: str) -> list[tuple[int, str]]:
    layout = {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
    return [
        (token.type, token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in layout
    ]


def test_media_protocol_bootstrap_and_runtime_match_pinned_main() -> None:
    manifest = json.loads((FIXTURES / "main_boundary.json").read_text(encoding="utf-8"))
    assert manifest["source_commit"] == MAIN_SHA
    assert manifest["hash_format"] == "git-blob-sha1"
    assert manifest["main_ci_blob_sha1"] == MAIN_CI_BLOB
    expected = manifest["files"]
    assert len(expected) == 89
    repair = json.loads((FIXTURES / "installer_repair_boundary.json").read_text(encoding="utf-8"))
    assert repair["base_commit"] == "53e0a6f032b6edaf755c4cd11b4f3eb3c2bcce2c"
    assert set(repair["files"]) == INSTALLER_REPAIR_FILES
    coexistence = json.loads((FIXTURES / "docker_coexistence_boundary.json").read_text("utf-8"))
    assert coexistence["base_commit"] == "f193191b1bc4ede0fade56d4936c1f485bbc373b"
    assert set(coexistence["files"]) == COEXISTENCE_FILES
    for relative, expected_blob in coexistence["files"].items():
        assert _git_blob(ROOT / relative) == expected_blob, relative
    for relative, expected_blob in expected.items():
        if relative in COEXISTENCE_FILES:
            continue
        if relative in INSTALLER_REPAIR_FILES:
            assert _git_blob(ROOT / relative) == repair["files"][relative], relative
            continue
        if relative == PREVIEW_FORMAT_EXCEPTION:
            fixture = FIXTURES / "main_preview.py.txt"
            assert _git_blob(fixture) == expected_blob
            original = fixture.read_text(encoding="utf-8")
            formatted = (ROOT / relative).read_text(encoding="utf-8")
            assert ast.dump(ast.parse(formatted)) == ast.dump(ast.parse(original))
            assert _code_tokens(formatted) == _code_tokens(original)
            continue
        assert _git_blob(ROOT / relative) == expected_blob, relative
    for directory in PROTECTED_TREES:
        actual_paths = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / directory).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        original_paths = {path for path in expected if path.startswith(f"{directory}/")}
        original_paths.update(
            path for path in COEXISTENCE_FILES if path.startswith(f"{directory}/")
        )
        assert actual_paths == original_paths, directory
    assert not (ROOT / "deploy/moblin-relay").exists()
    assert not (ROOT / "bootstrap_worker/relay_installer.py").exists()


def test_all_main_ci_gates_are_preserved_verbatim_and_in_order() -> None:
    fixture = FIXTURES / "main_ci.yml"
    assert _git_blob(fixture) == MAIN_CI_BLOB
    baseline = yaml.safe_load(fixture.read_text(encoding="utf-8"))
    candidate = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert {key: value for key, value in candidate.items() if key != "jobs"} == {
        key: value for key, value in baseline.items() if key != "jobs"
    }
    for name, original_job in baseline["jobs"].items():
        job = candidate["jobs"][name]
        assert {key: value for key, value in job.items() if key != "steps"} == {
            key: value for key, value in original_job.items() if key != "steps"
        }
        # Every original complete step must survive, including env/if/shell/uses.
        # Extra independent HUD checks are permitted; altered or skipped gates fail.
        cursor = 0
        for original_step in original_job["steps"]:
            matches = [
                index
                for index in range(cursor, len(job["steps"]))
                if job["steps"][index] == original_step
            ]
            assert matches, original_step.get("name", original_step.get("uses"))
            cursor = matches[0] + 1
