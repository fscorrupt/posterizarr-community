#!/usr/bin/env python3
"""Sync community gallery assets to GitHub Releases and maintain download stats.json."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import urllib.error
from urllib.parse import quote
import urllib.request

DEFAULT_REPO = "fscorrupt/posterizarr-community"

CATEGORIES = (
    (
        "blueprints",
        "gallery-blueprints",
        "sha256",
        "Community Gallery Blueprints (Assets)",
        "Rolling release hosting Community Gallery blueprint files for global download statistics.",
    ),
    (
        "dynamic_overlays",
        "gallery-dynamic-overlays",
        "bundle_sha256",
        "Community Gallery Dynamic Overlays (Assets)",
        "Rolling release hosting Community Gallery dynamic overlay bundles for global download statistics.",
    ),
)


class GitHubAPIError(RuntimeError):
    """Error returned by the GitHub REST API."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"GitHub API HTTP {status_code}: {message}")
        self.status_code = status_code


class GitHubReleaseClient:
    """Minimal stdlib GitHub Releases REST client."""

    def __init__(
        self,
        repo: str,
        token: str = "",
        api_base: str = "https://api.github.com",
        upload_base: str = "https://uploads.github.com",
    ) -> None:
        self.repo = repo
        self.token = token.strip()
        self.api_base = api_base.rstrip("/")
        self.upload_base = upload_base.rstrip("/")

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Posterizarr-Community-Stats-Sync",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra:
            headers.update(extra)
        return headers

    def _request_json(
        self,
        method: str,
        url: str,
        payload: dict | None = None,
        raw_bytes: bytes | None = None,
        content_type: str | None = None,
        accept_404: bool = False,
    ) -> object | None:
        data = raw_bytes
        extra_headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            extra_headers["Content-Type"] = "application/json; charset=utf-8"
        elif raw_bytes is not None:
            extra_headers["Content-Type"] = content_type or "application/octet-stream"
            extra_headers["Content-Length"] = str(len(raw_bytes))

        req = urllib.request.Request(url, data=data, headers=self._headers(extra_headers), method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status == 204:
                    return None
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if accept_404 and exc.code == 404:
                return None
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise GitHubAPIError(exc.code, detail or exc.reason) from exc

    def get_release_by_tag(self, tag: str) -> dict | None:
        url = f"{self.api_base}/repos/{self.repo}/releases/tags/{quote(tag, safe='')}"
        res = self._request_json("GET", url, accept_404=True)
        return res if isinstance(res, dict) else None

    def ensure_release(self, tag: str, title: str, body: str, target_commitish: str = "main") -> dict:
        existing = self.get_release_by_tag(tag)
        if existing is not None:
            return existing
        url = f"{self.api_base}/repos/{self.repo}/releases"
        try:
            created = self._request_json(
                "POST",
                url,
                payload={
                    "tag_name": tag,
                    "target_commitish": target_commitish,
                    "name": title,
                    "body": body,
                    "draft": False,
                    "prerelease": False,
                    "make_latest": "false",
                },
            )
            if isinstance(created, dict):
                return created
        except GitHubAPIError as exc:
            if exc.status_code == 422:
                retry = self.get_release_by_tag(tag)
                if retry is not None:
                    return retry
            raise
        raise RuntimeError(f"Failed to create or retrieve release '{tag}'")

    def list_release_assets(self, release_id: int) -> list[dict]:
        assets: list[dict] = []
        page = 1
        while True:
            url = (
                f"{self.api_base}/repos/{self.repo}/releases/{release_id}/assets"
                f"?per_page=100&page={page}"
            )
            batch = self._request_json("GET", url)
            if not isinstance(batch, list) or not batch:
                break
            assets.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                break
            page += 1
        return assets

    def delete_asset(self, asset_id: int) -> None:
        url = f"{self.api_base}/repos/{self.repo}/releases/assets/{asset_id}"
        self._request_json("DELETE", url)

    def upload_asset(self, release_id: int, asset_name: str, data: bytes, content_type: str) -> dict:
        url = f"{self.upload_base}/repos/{self.repo}/releases/{release_id}/assets?name={quote(asset_name, safe='')}"
        res = self._request_json(
            "POST",
            url,
            raw_bytes=data,
            content_type=content_type,
        )
        if not isinstance(res, dict):
            raise RuntimeError(f"Unexpected upload response for asset '{asset_name}'")
        return res


def empty_stats() -> dict:
    return {
        "schema_version": 1,
        "updated_at": None,
        "blueprints": {},
        "dynamic_overlays": {},
        "_tracking": {"blueprints": {}, "dynamic_overlays": {}},
    }


def _normalize_tracking_record(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    asset_id = raw.get("asset_id")
    if isinstance(asset_id, bool) or not isinstance(asset_id, int):
        asset_id = None
    asset_name = raw.get("asset_name") if isinstance(raw.get("asset_name"), str) else None
    sha256 = raw.get("sha256") if isinstance(raw.get("sha256"), str) else None
    baseline = raw.get("baseline")
    last_count = raw.get("last_release_count")
    return {
        "asset_id": asset_id,
        "asset_name": asset_name,
        "sha256": sha256,
        "baseline": max(0, baseline) if isinstance(baseline, int) and not isinstance(baseline, bool) else 0,
        "last_release_count": max(0, last_count) if isinstance(last_count, int) and not isinstance(last_count, bool) else 0,
    }


def load_stats(stats_path: Path) -> dict:
    """Load prior stats.json or return an empty baseline state."""
    base = empty_stats()
    if not stats_path.is_file():
        return base
    try:
        raw = json.loads(stats_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return base
    if not isinstance(raw, dict):
        return base

    if isinstance(raw.get("updated_at"), str):
        base["updated_at"] = raw["updated_at"]

    raw_tracking = raw.get("_tracking") if isinstance(raw.get("_tracking"), dict) else {}
    for category_key, _, _, _, _ in CATEGORIES:
        raw_counts = raw.get(category_key)
        if isinstance(raw_counts, dict):
            base[category_key] = {
                str(k): max(0, v)
                for k, v in raw_counts.items()
                if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
            }
        cat_track = raw_tracking.get(category_key)
        if isinstance(cat_track, dict):
            parsed_track: dict[str, dict] = {}
            for k, v in cat_track.items():
                if not isinstance(k, str):
                    continue
                norm = _normalize_tracking_record(v)
                if norm is not None:
                    parsed_track[k] = norm
            base["_tracking"][category_key] = parsed_track

    return base


def reconcile_asset_count(
    tracked: dict | None,
    live_asset: dict | None,
    sha256_override: str | None = None,
) -> tuple[int, dict]:
    """Combine preserved baseline with live GitHub Release download_count without double counting."""
    prev = tracked if isinstance(tracked, dict) else {}
    baseline = prev.get("baseline") if isinstance(prev.get("baseline"), int) and not isinstance(prev.get("baseline"), bool) else 0
    baseline = max(0, baseline)
    last_count = prev.get("last_release_count") if isinstance(prev.get("last_release_count"), int) and not isinstance(prev.get("last_release_count"), bool) else 0
    last_count = max(0, last_count)
    prev_id = prev.get("asset_id")
    asset_name = prev.get("asset_name")
    sha_val = sha256_override if sha256_override is not None else prev.get("sha256")

    if live_asset is None:
        total = baseline + last_count
        return total, {
            "asset_id": prev_id,
            "asset_name": asset_name,
            "sha256": sha_val,
            "baseline": baseline,
            "last_release_count": last_count,
        }

    live_id = live_asset.get("id")
    live_name = live_asset.get("name") or asset_name
    raw_dl = live_asset.get("download_count")
    live_count = max(0, raw_dl) if isinstance(raw_dl, int) and not isinstance(raw_dl, bool) else 0

    if prev_id is not None and live_id is not None and prev_id != live_id:
        baseline = baseline + last_count
        new_last = live_count
    elif prev_id is not None and live_id == prev_id:
        new_last = max(last_count, live_count)
    else:
        new_last = live_count

    total = baseline + new_last
    return total, {
        "asset_id": live_id,
        "asset_name": live_name,
        "sha256": sha_val,
        "baseline": baseline,
        "last_release_count": new_last,
    }


def _asset_content_type(asset_name: str) -> str:
    return "application/zip" if asset_name.lower().endswith(".zip") else "application/json"


def sync_category_assets(
    client: GitHubReleaseClient,
    repo_root: Path,
    release_tag: str,
    release_title: str,
    release_body: str,
    sha_key: str,
    entries: list[dict],
    tracking_map: dict[str, dict],
) -> list[dict]:
    """Ensure GitHub Release assets match index entries and fold counts before replacement."""
    release = client.ensure_release(release_tag, release_title, release_body)
    release_id = release["id"]
    live_assets = client.list_release_assets(release_id)

    live_by_id: dict[int, dict] = {
        asset["id"]: asset for asset in live_assets if isinstance(asset.get("id"), int)
    }
    live_by_name: dict[str, dict] = {
        str(asset["name"]).casefold(): asset
        for asset in live_assets
        if isinstance(asset.get("name"), str)
    }
    deleted_ids: set[int] = set()
    expected_names: set[str] = set()
    resulting_assets: dict[int, dict] = dict(live_by_id)

    for entry in entries:
        entry_id = entry.get("id")
        rel_file = entry.get("file")
        asset_name = entry.get("release_asset")
        entry_sha = entry.get(sha_key)
        if not all(isinstance(v, str) and v for v in (entry_id, rel_file, asset_name)):
            continue

        expected_names.add(asset_name.casefold())
        tracked = tracking_map.get(entry_id)
        old_by_name = live_by_name.get(asset_name.casefold())
        old_by_id = (
            live_by_id.get(tracked.get("asset_id"))
            if isinstance(tracked, dict) and isinstance(tracked.get("asset_id"), int)
            else None
        )

        matches_existing = old_by_name is not None and (
            (isinstance(tracked, dict) and tracked.get("sha256") == entry_sha)
            or (isinstance(entry_sha, str) and old_by_name.get("digest") == f"sha256:{entry_sha}")
        )

        if matches_existing and old_by_name is not None:
            if old_by_id is not None and old_by_id.get("id") != old_by_name.get("id"):
                # Clean up stale previous asset (e.g. extension change left an older file behind)
                fold_total, tracked = reconcile_asset_count(tracked, old_by_id, sha256_override=entry_sha)
                tracked = {
                    **tracked,
                    "baseline": fold_total,
                    "last_release_count": 0,
                    "asset_id": old_by_name.get("id"),
                }
                client.delete_asset(old_by_id["id"])
                deleted_ids.add(old_by_id["id"])
                resulting_assets.pop(old_by_id["id"], None)

            _, updated_track = reconcile_asset_count(tracked, old_by_name, sha256_override=entry_sha)
            tracking_map[entry_id] = updated_track
            continue

        # Upload needed (new asset or content/filename changed): preserve old download_count first.
        primary_old = old_by_id or old_by_name
        new_baseline, _ = reconcile_asset_count(tracked, primary_old, sha256_override=entry_sha)
        if old_by_id is not None and old_by_name is not None and old_by_id.get("id") != old_by_name.get("id"):
            extra_dl = old_by_name.get("download_count")
            if isinstance(extra_dl, int) and not isinstance(extra_dl, bool) and extra_dl > 0:
                new_baseline += extra_dl

        for old_asset in (old_by_id, old_by_name):
            if old_asset is not None and old_asset.get("id") not in deleted_ids:
                client.delete_asset(old_asset["id"])
                deleted_ids.add(old_asset["id"])
                resulting_assets.pop(old_asset["id"], None)

        file_path = (repo_root / rel_file).resolve()
        if not file_path.is_file():
            raise FileNotFoundError(f"Missing community entry file: {file_path}")
        payload = file_path.read_bytes()
        uploaded = client.upload_asset(release_id, asset_name, payload, _asset_content_type(asset_name))
        if isinstance(uploaded.get("id"), int):
            resulting_assets[uploaded["id"]] = uploaded
        tracking_map[entry_id] = {
            "asset_id": uploaded.get("id"),
            "asset_name": uploaded.get("name") or asset_name,
            "sha256": entry_sha if isinstance(entry_sha, str) else None,
            "baseline": new_baseline,
            "last_release_count": 0,
        }

    # Prune orphaned release assets while retaining historical counts in _tracking
    track_by_asset_id: dict[int, str] = {}
    track_by_asset_name: dict[str, str] = {}
    for eid, rec in tracking_map.items():
        if isinstance(rec.get("asset_id"), int):
            track_by_asset_id[rec["asset_id"]] = eid
        if isinstance(rec.get("asset_name"), str):
            track_by_asset_name[rec["asset_name"].casefold()] = eid

    for asset in live_assets:
        aid = asset.get("id")
        aname = str(asset.get("name") or "")
        if not isinstance(aid, int) or aid in deleted_ids:
            continue
        if aname.casefold() in expected_names:
            continue
        owner_id = track_by_asset_id.get(aid) or track_by_asset_name.get(aname.casefold())
        if owner_id is not None:
            final_total, updated_rec = reconcile_asset_count(tracking_map.get(owner_id), asset, sha256_override=None)
            updated_rec["asset_id"] = None
            updated_rec["sha256"] = None
            updated_rec["baseline"] = final_total
            updated_rec["last_release_count"] = 0
            tracking_map[owner_id] = updated_rec
        client.delete_asset(aid)
        deleted_ids.add(aid)
        resulting_assets.pop(aid, None)

    return list(resulting_assets.values())


def compute_stats_document(
    index_data: dict,
    live_assets_by_category: dict[str, list[dict]],
    prev_stats: dict,
    now_iso: str,
) -> tuple[dict, bool]:
    """Compute updated stats.json document and whether meaningful values changed."""
    prev_tracking = prev_stats.get("_tracking") if isinstance(prev_stats.get("_tracking"), dict) else {}
    new_stats = {
        "schema_version": 1,
        "updated_at": now_iso,
        "blueprints": {},
        "dynamic_overlays": {},
        "_tracking": copy.deepcopy(
            {
                "blueprints": prev_tracking.get("blueprints", {}),
                "dynamic_overlays": prev_tracking.get("dynamic_overlays", {}),
            }
        ),
    }

    for category_key, _, sha_key, _, _ in CATEGORIES:
        entries = index_data.get(category_key) or []
        live_assets = live_assets_by_category.get(category_key) or []
        by_name = {
            str(asset["name"]).casefold(): asset
            for asset in live_assets
            if isinstance(asset, dict) and isinstance(asset.get("name"), str)
        }
        by_id = {
            asset["id"]: asset
            for asset in live_assets
            if isinstance(asset, dict) and isinstance(asset.get("id"), int)
        }
        cat_tracking = new_stats["_tracking"][category_key]

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id")
            asset_name = entry.get("release_asset")
            entry_sha = entry.get(sha_key)
            if not isinstance(entry_id, str) or not entry_id:
                continue

            tracked = cat_tracking.get(entry_id)
            live_asset = None
            if isinstance(asset_name, str) and asset_name:
                live_asset = by_name.get(asset_name.casefold())
            if live_asset is None and isinstance(tracked, dict) and isinstance(tracked.get("asset_id"), int):
                live_asset = by_id.get(tracked["asset_id"])

            sha_override = entry_sha if isinstance(entry_sha, str) else None
            total, updated_rec = reconcile_asset_count(tracked, live_asset, sha256_override=sha_override)
            new_stats[category_key][entry_id] = total
            cat_tracking[entry_id] = updated_rec

    def _comparable(doc: dict) -> dict:
        return {
            "schema_version": doc.get("schema_version"),
            "blueprints": doc.get("blueprints", {}),
            "dynamic_overlays": doc.get("dynamic_overlays", {}),
            "_tracking": doc.get("_tracking", {}),
        }

    changed = (
        not isinstance(prev_stats.get("updated_at"), str)
        or _comparable(new_stats) != _comparable(prev_stats)
    )
    if not changed:
        new_stats["updated_at"] = prev_stats["updated_at"]

    return new_stats, changed


def run_sync(
    client: GitHubReleaseClient,
    repo_root: Path,
    index_path: Path,
    stats_path: Path,
    sync_assets: bool,
    update_stats: bool,
    now_iso: str | None = None,
) -> bool:
    """Execute asset sync and/or stats generation. Returns True if stats.json was written."""
    index_data = json.loads(index_path.read_text(encoding="utf-8"))
    working_stats = load_stats(stats_path)
    live_assets_by_category: dict[str, list[dict]] = {}

    for category_key, release_tag, sha_key, title, body in CATEGORIES:
        entries = index_data.get(category_key) or []
        cat_tracking = working_stats["_tracking"].setdefault(category_key, {})
        if sync_assets:
            live_assets = sync_category_assets(
                client=client,
                repo_root=repo_root,
                release_tag=release_tag,
                release_title=title,
                release_body=body,
                sha_key=sha_key,
                entries=entries,
                tracking_map=cat_tracking,
            )
            live_assets_by_category[category_key] = live_assets
        elif update_stats:
            release = client.get_release_by_tag(release_tag)
            live_assets_by_category[category_key] = (
                client.list_release_assets(release["id"]) if release is not None else []
            )

    if not update_stats:
        return False

    timestamp = now_iso or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    updated_doc, changed = compute_stats_document(
        index_data=index_data,
        live_assets_by_category=live_assets_by_category,
        prev_stats=working_stats if not sync_assets else load_stats(stats_path),
        now_iso=timestamp,
    )
    # Wait: if sync_assets updated working_stats["_tracking"], compute_stats_document needs the synced _tracking as its input baseline,
    # while comparing against the on-disk prev_stats to decide `changed`!
    if sync_assets:
        disk_prev = load_stats(stats_path)
        updated_doc, _ = compute_stats_document(
            index_data=index_data,
            live_assets_by_category=live_assets_by_category,
            prev_stats=working_stats,
            now_iso=timestamp,
        )
        changed = (
            not isinstance(disk_prev.get("updated_at"), str)
            or not stats_path.is_file()
            or updated_doc.get("blueprints") != disk_prev.get("blueprints")
            or updated_doc.get("dynamic_overlays") != disk_prev.get("dynamic_overlays")
            or updated_doc.get("_tracking") != disk_prev.get("_tracking")
        )

    if changed or not stats_path.is_file():
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(updated_doc, indent=2, ensure_ascii=False) + "\n"
        stats_path.write_text(rendered, encoding="utf-8")
        print(
            f"Wrote {stats_path} "
            f"({len(updated_doc['blueprints'])} blueprint(s), "
            f"{len(updated_doc['dynamic_overlays'])} dynamic-overlay(s))."
        )
        return True

    print("stats.json is already up to date.")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync Posterizarr Community Gallery assets to GitHub Releases and maintain stats.json."
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO),
        help="GitHub repository slug (owner/name).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Path to posterizarr-community checkout.",
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=None,
        help="Path to index.json (defaults to <repo-root>/index.json).",
    )
    parser.add_argument(
        "--stats-file",
        type=Path,
        required=True,
        help="Path to read/write stats.json.",
    )
    parser.add_argument("--sync-assets", action="store_true", help="Upload/update/prune GitHub Release assets.")
    parser.add_argument("--update-stats", action="store_true", help="Write updated download stats.json.")
    args = parser.parse_args()

    if not args.sync_assets and not args.update_stats:
        parser.error("Specify at least one of --sync-assets or --update-stats.")

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if args.sync_assets and not token:
        print("ERROR: GITHUB_TOKEN is required for --sync-assets.", file=sys.stderr)
        return 1

    repo_root = args.repo_root.resolve()
    index_path = (args.index or (repo_root / "index.json")).resolve()
    if not index_path.is_file():
        print(f"ERROR: index.json not found at {index_path}", file=sys.stderr)
        return 1

    client = GitHubReleaseClient(repo=args.repo, token=token)
    run_sync(
        client=client,
        repo_root=repo_root,
        index_path=index_path,
        stats_path=args.stats_file.resolve(),
        sync_assets=args.sync_assets,
        update_stats=args.update_stats,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
