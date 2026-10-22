#!/usr/bin/env python3
"""Root-owned forced SSH command for one immutable GitHub deployment.

Install this file and the two deployment tools under /srv/stock-watch/deploy-tools
as root, without group/other write permission. The dedicated authorized_keys entry
must use: command="/usr/bin/python3 -I /srv/stock-watch/deploy-tools/github-gate.py",restrict

This host-side entrypoint intentionally supports Python 3.8; the application and
GitHub tests still use Python 3.12. No request can supply files or shell commands.
"""

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


BASE = Path("/srv/stock-watch")
MAX_REQUEST_BYTES = 16384
SAFE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
FIELDS = {"schema", "image", "sha", "registry_user", "registry_token"}
IMAGE = re.compile(r"ghcr\.io/qinyinxiaoyun/stockwatcher@sha256:[0-9a-f]{64}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
USER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}(?:\[bot\])?\Z")
TOKEN = re.compile(r"[\x20-\x7e]{1,512}\Z")


class GateError(Exception):
    """A fixed, safe error message; never include request or subprocess data."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GateError("Invalid deployment request")
        result[key] = value
    return result


def read_request(stream):
    raw = stream.read(MAX_REQUEST_BYTES + 1)
    if not isinstance(raw, bytes) or len(raw) > MAX_REQUEST_BYTES:
        raise GateError("Invalid deployment request")
    try:
        request = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError):
        raise GateError("Invalid deployment request") from None
    if not isinstance(request, dict) or set(request) != FIELDS:
        raise GateError("Invalid deployment request")
    if type(request["schema"]) is not int or request["schema"] != 1:
        raise GateError("Invalid deployment request")
    for key, pattern in (("image", IMAGE), ("sha", SHA), ("registry_user", USER), ("registry_token", TOKEN)):
        if not isinstance(request[key], str) or not pattern.fullmatch(request[key]):
            raise GateError("Invalid deployment request")
    return request


def _path_stat(path):
    return os.lstat(str(path))


def _descriptor_stat(descriptor):
    return os.fstat(descriptor)


def _check_metadata(info, directory):
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if info.st_uid != 0 or info.st_mode & 0o022 or not expected(info.st_mode):
        raise GateError("Unsafe deployment filesystem")


def _check_path(path, directory):
    if not path.is_absolute() or ".." in path.parts:
        raise GateError("Unsafe deployment filesystem")
    for parent in reversed(path.parents):
        _check_metadata(_path_stat(parent), True)
    _check_metadata(_path_stat(path), directory)


def _ensure_directory(path):
    _check_path(path.parent, True)
    try:
        os.mkdir(str(path), 0o750)
    except FileExistsError:
        pass
    _check_path(path, True)


def _read_controlled_file(path):
    _check_path(path, False)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(str(path), flags)
    try:
        _check_metadata(_descriptor_stat(descriptor), False)
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            return source.read()
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _install_controlled_file(path, contents):
    _check_path(path.parent, True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags, 0o640)
    except FileExistsError:
        # Releases are immutable; retries can reuse only the exact trusted bytes.
        if _read_controlled_file(path) != contents:
            raise GateError("Existing deployment tools differ") from None
        return
    try:
        _check_metadata(_descriptor_stat(descriptor), False)
        with os.fdopen(descriptor, "wb") as target:
            descriptor = None
            target.write(contents)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _check_path(path, False)


def prepare_release(sha):
    tools = BASE / "deploy-tools"
    _check_path(tools, True)
    compose = _read_controlled_file(tools / "compose.registry.yaml")
    script = _read_controlled_file(tools / "deploy-image.sh")
    releases = BASE / "image-releases"
    release = releases / sha
    _ensure_directory(releases)
    _ensure_directory(release)
    _ensure_directory(release / "scripts")
    _install_controlled_file(release / "compose.registry.yaml", compose)
    _install_controlled_file(release / "scripts" / "deploy-image.sh", script)
    return release


def run_gate(command, stream):
    if command != "deploy":
        raise GateError("Only the deploy command is permitted")
    request = read_request(stream)
    if getattr(os, "geteuid", lambda: -1)() != 0:
        raise GateError("Deployment gate must run as root")
    try:
        release = prepare_release(request["sha"])
        # Never inherit TMPDIR, BASH_ENV, PYTHONPATH, Docker overrides or queue
        # approval from the SSH process. Credentials live only in this directory.
        with tempfile.TemporaryDirectory(prefix="stock-watch-ghcr-", dir="/tmp") as config:
            os.chmod(config, 0o700)
            info = _path_stat(Path(config))
            _check_metadata(info, True)
            if info.st_mode & 0o077:
                raise GateError("Unsafe temporary Docker configuration")
            environment = {"PATH": SAFE_PATH, "DOCKER_CONFIG": config}
            result = subprocess.run(
                ["/usr/bin/docker", "login", "ghcr.io", "--username", request["registry_user"], "--password-stdin"],
                input=(request["registry_token"] + "\n").encode("ascii"),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=environment, cwd=str(release), timeout=120, check=False,
            )
            if result.returncode != 0:
                raise GateError("Registry login failed")
            result = subprocess.run(
                ["/usr/bin/bash", str(release / "scripts" / "deploy-image.sh"), request["image"], request["sha"]],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=environment, cwd=str(release), timeout=900, check=False,
            )
            if result.returncode != 0:
                raise GateError("Image deployment failed")
    except (OSError, subprocess.SubprocessError):
        raise GateError("Deployment operation failed") from None


def main():
    try:
        run_gate(os.environ.get("SSH_ORIGINAL_COMMAND", ""), sys.stdin.buffer)
    except GateError as error:
        print(str(error), file=sys.stderr)
        return 1
    print("Image deployment ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
