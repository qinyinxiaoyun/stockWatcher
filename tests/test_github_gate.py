"""Offline tests for the root-owned SSH deployment gate; no external commands."""

import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest


GATE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "github-gate.py"
SPEC = importlib.util.spec_from_file_location("github_gate", GATE_PATH)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


@pytest.fixture
def request_data():
    return {
        "schema": 1,
        "image": "ghcr.io/qinyinxiaoyun/stockwatcher@sha256:" + "a" * 64,
        "sha": "b" * 40,
        "registry_user": "github-actions[bot]",
        "registry_token": "offline-only-placeholder",
    }


def request_stream(request):
    return io.BytesIO(json.dumps(request).encode("utf-8"))


@pytest.fixture
def host(tmp_path, monkeypatch):
    """Emulate the Linux root-owned filesystem even on a Windows test host."""
    base = tmp_path / "srv" / "stock-watch"
    tools = base / "deploy-tools"
    tools.mkdir(parents=True)
    (tools / "compose.registry.yaml").write_bytes(b"services: {}\n")
    (tools / "deploy-image.sh").write_bytes(b"#!/bin/bash\nexit 0\n")
    monkeypatch.setattr(gate, "BASE", base)
    monkeypatch.setattr(gate.os, "geteuid", lambda: 0, raising=False)
    original_lstat = gate._path_stat
    original_fstat = gate._descriptor_stat

    def root_metadata(info, path=None):
        mode = stat.S_IFMT(info.st_mode)
        if stat.S_ISDIR(mode):
            mode |= 0o700 if path is not None and path.name.startswith("stock-watch-ghcr-") else 0o750
        else:
            mode |= 0o640
        return SimpleNamespace(st_mode=mode, st_uid=0)

    monkeypatch.setattr(gate, "_path_stat", lambda path: root_metadata(original_lstat(path), path))
    monkeypatch.setattr(gate, "_descriptor_stat", lambda fd: root_metadata(original_fstat(fd)))
    temporary_directories = []
    real_temporary_directory = gate.tempfile.TemporaryDirectory

    def temporary_directory(**kwargs):
        assert kwargs == {"prefix": "stock-watch-ghcr-", "dir": "/tmp"}
        directory = real_temporary_directory(prefix=kwargs["prefix"], dir=tmp_path)
        temporary_directories.append(Path(directory.name))
        return directory

    monkeypatch.setattr(gate.tempfile, "TemporaryDirectory", temporary_directory)
    calls = []

    def run(argv, **kwargs):
        config = Path(kwargs["env"]["DOCKER_CONFIG"])
        assert config.is_dir()
        (config / "config.json").write_text("offline registry credentials", encoding="utf-8")
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gate.subprocess, "run", run)
    return SimpleNamespace(base=base, tools=tools, calls=calls, configs=temporary_directories)


@pytest.mark.parametrize("command", ["", "deploy ", " deploy", "deploy\n", "deploy; id", "bash", "scp -t /tmp/file"])
def test_only_exact_deploy_command_is_accepted(command):
    with pytest.raises(gate.GateError, match="Only the deploy command"):
        gate.run_gate(command, io.BytesIO(b"not read"))


@pytest.mark.parametrize("key,value", [
    ("schema", True), ("schema", 1.0), ("schema", 2),
    ("image", "ghcr.io/another/repository@sha256:" + "a" * 64),
    ("image", "ghcr.io/qinyinxiaoyun/stockwatcher:latest"),
    ("image", "ghcr.io/qinyinxiaoyun/stockwatcher@sha256:" + "A" * 64),
    ("image", "ghcr.io/qinyinxiaoyun/stockwatcher@sha256:" + "a" * 64 + "\n"),
    ("sha", "../escape"), ("sha", "b" * 40 + ";id"), ("sha", "B" * 40),
    ("registry_user", "-name"), ("registry_user", "name-"),
    ("registry_user", "a--b"), ("registry_user", "a" * 40),
    ("registry_user", "name;id"), ("registry_user", "name\n"),
    ("registry_token", ""), ("registry_token", "a" * 513),
    ("registry_token", "a\nb"), ("registry_token", "a\rb"),
    ("registry_token", "a\x00b"), ("registry_token", "非ASCII"),
    ("registry_token", 123),
])
def test_rejects_invalid_fields(request_data, key, value):
    request_data[key] = value
    with pytest.raises(gate.GateError, match="Invalid deployment request"):
        gate.read_request(request_stream(request_data))


@pytest.mark.parametrize("extra", ["command", "compose", "script", "WORKER_QUEUE_APPROVED", "env"])
def test_extra_fields_cannot_supply_code_or_queue_approval(request_data, extra):
    request_data[extra] = "untrusted input"
    with pytest.raises(gate.GateError, match="Invalid deployment request"):
        gate.read_request(request_stream(request_data))


@pytest.mark.parametrize("raw", [b"{}", b"[]", b"null", b"{", b"\xff", b"{\"schema\":1,\"schema\":1}", b" " * 16385])
def test_rejects_invalid_json_duplicate_fields_and_oversized_requests(raw):
    with pytest.raises(gate.GateError, match="Invalid deployment request"):
        gate.read_request(io.BytesIO(raw))


def test_bounded_input_read():
    class Stream:
        def read(self, count):
            assert count == 16385
            return b" " * count

    with pytest.raises(gate.GateError):
        gate.read_request(Stream())


@pytest.mark.parametrize("user", ["a", "qinyinxiaoyun", "a-b", "a" * 39, "github-actions[bot]"])
def test_accepts_github_account_names(request_data, user):
    request_data["registry_user"] = user
    assert gate.read_request(request_stream(request_data))["registry_user"] == user


def test_success_only_runs_fixed_commands_and_cleans_credentials(host, request_data, monkeypatch):
    monkeypatch.setenv("WORKER_QUEUE_APPROVED", "true")
    monkeypatch.setenv("BASH_ENV", "/untrusted/script")
    monkeypatch.setenv("DOCKER_HOST", "untrusted")
    gate.run_gate("deploy", request_stream(request_data))
    release = host.base / "image-releases" / request_data["sha"]
    assert (release / "compose.registry.yaml").read_bytes() == (host.tools / "compose.registry.yaml").read_bytes()
    assert (release / "scripts" / "deploy-image.sh").read_bytes() == (host.tools / "deploy-image.sh").read_bytes()
    assert len(host.calls) == 2
    login, deploy = host.calls
    assert login[0] == ["/usr/bin/docker", "login", "ghcr.io", "--username", request_data["registry_user"], "--password-stdin"]
    assert login[1]["input"] == (request_data["registry_token"] + "\n").encode("ascii")
    assert deploy[0] == ["/usr/bin/bash", str(release / "scripts" / "deploy-image.sh"), request_data["image"], request_data["sha"]]
    assert deploy[1]["stdin"] == subprocess.DEVNULL
    for argv, kwargs in host.calls:
        assert request_data["registry_token"] not in repr(argv)
        assert kwargs.get("shell", False) is False
        assert kwargs["cwd"] == str(release)
        assert set(kwargs["env"]) == {"PATH", "DOCKER_CONFIG"}
        assert kwargs["env"]["PATH"] == gate.SAFE_PATH
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    assert host.configs and all(not path.exists() for path in host.configs)


@pytest.mark.parametrize("failure", ["returncode", "exception", "timeout"])
def test_login_failure_never_deploys_or_leaks_token(host, request_data, monkeypatch, failure, capsys):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if failure == "exception":
            raise OSError(request_data["registry_token"])
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 120, output=request_data["registry_token"])
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(gate.subprocess, "run", run)
    with pytest.raises(gate.GateError) as error:
        gate.run_gate("deploy", request_stream(request_data))
    assert len(calls) == 1
    assert request_data["registry_token"] not in str(error.value)
    assert request_data["registry_token"] not in repr(calls)
    assert request_data["registry_token"] not in str(capsys.readouterr())
    assert all(not path.exists() for path in host.configs)


def test_deployment_failure_cleans_credentials(host, request_data, monkeypatch):
    def run(argv, **kwargs):
        host.calls.append((argv, kwargs))
        return SimpleNamespace(returncode=1 if argv[0] == "/usr/bin/bash" else 0)

    monkeypatch.setattr(gate.subprocess, "run", run)
    with pytest.raises(gate.GateError, match="Image deployment failed"):
        gate.run_gate("deploy", request_stream(request_data))
    assert len(host.calls) == 2
    assert all(not path.exists() for path in host.configs)


@pytest.mark.parametrize("target", ["ancestor", "tools", "source", "releases", "release", "scripts", "destination"])
@pytest.mark.parametrize("unsafe", ["symlink", "nonroot", "group_write", "other_write"])
def test_each_filesystem_component_must_be_root_controlled(host, request_data, monkeypatch, target, unsafe):
    release = host.base / "image-releases" / request_data["sha"]
    (release / "scripts").mkdir(parents=True)
    (release / "compose.registry.yaml").write_bytes((host.tools / "compose.registry.yaml").read_bytes())
    path = {
        "ancestor": host.base.parent,
        "tools": host.tools,
        "source": host.tools / "deploy-image.sh",
        "releases": host.base / "image-releases",
        "release": release,
        "scripts": release / "scripts",
        "destination": release / "compose.registry.yaml",
    }[target]
    original = gate._path_stat

    def changed_stat(candidate):
        info = original(candidate)
        if candidate != path:
            return info
        mode = info.st_mode
        if unsafe == "symlink":
            mode = stat.S_IFLNK | 0o777
        elif unsafe == "group_write":
            mode |= 0o020
        elif unsafe == "other_write":
            mode |= 0o002
        return SimpleNamespace(st_mode=mode, st_uid=1000 if unsafe == "nonroot" else 0)

    monkeypatch.setattr(gate, "_path_stat", changed_stat)
    with pytest.raises(gate.GateError, match="Unsafe deployment filesystem"):
        gate.run_gate("deploy", request_stream(request_data))
    assert not host.calls
    assert not host.configs


def test_nonroot_execution_is_rejected(host, request_data, monkeypatch):
    monkeypatch.setattr(gate.os, "geteuid", lambda: 1000)
    with pytest.raises(gate.GateError, match="must run as root"):
        gate.run_gate("deploy", request_stream(request_data))
    assert not host.calls


def test_release_retry_reuses_identical_files(host, request_data):
    gate.run_gate("deploy", request_stream(request_data))
    gate.run_gate("deploy", request_stream(request_data))
    assert len(host.calls) == 4
    assert all(not path.exists() for path in host.configs)


def test_release_cannot_be_replaced_with_different_tools(host, request_data):
    gate.run_gate("deploy", request_stream(request_data))
    (host.tools / "deploy-image.sh").write_bytes(b"new trusted script\n")
    with pytest.raises(gate.GateError, match="Existing deployment tools differ"):
        gate.run_gate("deploy", request_stream(request_data))
    assert len(host.calls) == 2


def test_main_emits_only_fixed_messages_on_invalid_input(request_data, monkeypatch, capsys):
    request_data["command"] = request_data["registry_token"]
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "deploy")
    monkeypatch.setattr(gate.sys, "stdin", SimpleNamespace(buffer=request_stream(request_data)))
    assert gate.main() == 1
    output = capsys.readouterr()
    assert output.err == "Invalid deployment request\n"
    assert request_data["registry_token"] not in output.err


def test_host_entrypoint_parses_as_python38():
    ast.parse(GATE_PATH.read_text(encoding="utf-8"), feature_version=(3, 8))
