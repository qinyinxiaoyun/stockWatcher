"""Build an allow-listed source release without credentials or runtime data."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile


def main() -> None:
    root = Path(__file__).resolve().parents[1]

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    runtime_paths = ["Dockerfile", "pyproject.toml", "README.md", ".dockerignore", "stock_watch"]
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", *runtime_paths], cwd=root, check=True)
    sha = git("rev-parse", "HEAD")
    names = [name for name in git("ls-files", "stock_watch").splitlines() if name.endswith(".py")]
    names += [
        "Dockerfile", "pyproject.toml", "README.md", ".dockerignore",
        "compose.host-nginx.yaml", "deploy/nginx/stock-watch.conf",
        "scripts/deploy-host-nginx.sh", "scripts/package-release.py",
    ]
    files: dict[str, bytes] = {}
    for name in sorted(set(names)):
        source = root / name
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError(f"Invalid release path: {name}")
        files[name] = source.read_bytes().replace(b"\r\n", b"\n")

    manifest = {"schema": 1, "code_sha": sha, "files": {
        name: hashlib.sha256(content).hexdigest() for name, content in files.items()
    }}
    files["release-manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    destination = root / "dist" / f"release-{sha}.tar.gz"
    destination.parent.mkdir(exist_ok=True)
    with tarfile.open(destination, "w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            archive.addfile(info, io.BytesIO(content))
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(destination.suffix + ".sha256").write_text(
        f"{digest}  {destination.name}\n", encoding="ascii", newline="\n"
    )
    print(json.dumps({"archive": str(destination), "sha256": digest, "code_sha": sha}))


if __name__ == "__main__":
    main()
