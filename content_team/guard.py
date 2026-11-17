"""Hard guard: content-team code must never publish or send a reply."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable

PACK_DIR = Path(__file__).resolve().parent

# Names and URL fragments that would mean this package can post or hit the X API.
FORBIDDEN_NAMES = frozenset({
    "publish_post_to_platforms",
    "publish_twitter",
    "publish_linkedin",
    "publish_instagram_post",
    "publish_tiktok_post",
    "publish_facebook_post",
    "publish_youtube_short",
    "publish_threads_post",
    "publish_via_postproxy",
    "execute_autonomous_autopost",
    "execute_scheduled_post",
    "fetch_and_draft_mention_replies",
    "fetch_and_draft_mention_replies_with_bearer",
    "tweepy",
})
FORBIDDEN_SUBSTRINGS = (
    "api.twitter.com",
    "api.x.com",
    "publish.twitter.com",
)


class ContentTeamGuardError(RuntimeError):
    """Raised when a publish or reply path is attempted or detected."""


def blocked(*args, **kwargs):
    raise ContentTeamGuardError(
        "content_team_guard: publish and reply are forbidden. Drafts stay in the review queue."
    )


def _iter_python_files(root: Path = PACK_DIR) -> Iterable[Path]:
    for path in root.rglob("*.py"):
        if path.name.endswith(".pyc"):
            continue
        yield path


def scan_package(root: Path = PACK_DIR) -> list[str]:
    """Return human-readable violations (empty means the package is clean)."""
    hits: list[str] = []
    for path in _iter_python_files(root):
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(root)
        # guard.py and web.py name the blocked hosts in order to forbid them.
        if path.name not in {"guard.py", "web.py"}:
            for needle in FORBIDDEN_SUBSTRINGS:
                if needle in source:
                    hits.append(f"{rel}: contains {needle}")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            hits.append(f"{rel}: cannot parse ({exc})")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name.split(".", 1)[0]
                    if name in FORBIDDEN_NAMES:
                        hits.append(f"{rel}: imports {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                root_mod = module.split(".", 1)[0]
                if root_mod in FORBIDDEN_NAMES or "tweepy" in module:
                    hits.append(f"{rel}: imports from {module}")
                for alias in node.names:
                    if alias.name in FORBIDDEN_NAMES:
                        hits.append(f"{rel}: imports {alias.name} from {module}")
            elif isinstance(node, ast.Call):
                func = node.func
                name = None
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name in FORBIDDEN_NAMES:
                    hits.append(f"{rel}: calls {name}()")
    return hits


def assert_no_publish_hooks(root: Path = PACK_DIR) -> None:
    hits = scan_package(root)
    if hits:
        raise ContentTeamGuardError("content_team_guard violations:\n- " + "\n- ".join(hits))
