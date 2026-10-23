"""Guards for the HyperFrames render runtime installed from package.json / package-lock.json."""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def _ver(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def test_renderer_npx_version_matches_pinned_package():
    pinned = json.loads(_load("package.json"))["dependencies"]["hyperframes"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", pinned), "pin hyperframes exactly so npx reuses /app/node_modules"
    calls = re.findall(r'"hyperframes@([^"]+)"', _load("template_renderer.py"))
    assert calls and set(calls) == {pinned}


def test_lockfile_matches_package_json_and_has_patched_transitive_deps():
    lock = json.loads(_load("package-lock.json"))
    pkgs = lock["packages"]
    assert pkgs[""]["dependencies"] == json.loads(_load("package.json"))["dependencies"]
    assert pkgs["node_modules/hyperframes"]["version"] == "0.7.40"
    # First patched versions for the advisories npm audit reported on hyperframes 0.7.40's tree.
    floors = {"sharp": "0.35.5", "adm-zip": "0.6.1", "puppeteer-core": "24.43.2", "@puppeteer/browsers": "3.0.0"}
    for name, floor in floors.items():
        for path, meta in pkgs.items():
            if path.endswith("node_modules/" + name):
                assert _ver(meta["version"]) >= _ver(floor), (path, meta["version"])
    for gone in ("extract-zip", "basic-ftp"):  # no fixed release exists; must not be in the tree
        assert not any(p.endswith("node_modules/" + gone) for p in pkgs)


def test_dockerfile_installs_from_lockfile():
    docker = _load("Dockerfile")
    assert "COPY package.json package-lock.json ./" in docker
    assert "npm ci --omit=dev" in docker
    assert "npm install hyperframes" not in docker
    assert "node_modules" in _load(".dockerignore")
