#!/usr/bin/env python3
"""Validate Posterizarr community submissions and build index.json."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
BLUEPRINTS_DIR = REPO_ROOT / "blueprints"
OVERLAYS_DIR = REPO_ROOT / "dynamic-overlays"
INDEX_PATH = REPO_ROOT / "index.json"

REPO_SLUG = "fscorrupt/posterizarr-community"
REPO_BRANCH = "main"
BLUEPRINTS_RELEASE_TAG = "gallery-blueprints"
OVERLAYS_RELEASE_TAG = "gallery-dynamic-overlays"
MAX_RELEASE_ASSETS = 1000

AUTHOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,48}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,29}$")
SAFE_RELEASE_SEGMENT_RE = re.compile(r"[^a-z0-9._-]+")

PREVIEW_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_PREVIEW_BYTES = 2 * 1024 * 1024
MAX_BLUEPRINT_BYTES = 256 * 1024
MAX_BLUEPRINT_BUNDLE_BYTES = 25 * 1024 * 1024
MAX_BLUEPRINT_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_BLUEPRINT_ASSET_BYTES = 10 * 1024 * 1024
MAX_BLUEPRINT_BUNDLE_FILES = 64
BLUEPRINT_BUNDLE_KIND = "posterizarr-blueprint-bundle"
BLUEPRINT_BUNDLE_VERSION = 1
BLUEPRINT_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
BLUEPRINT_FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2"}
BLUEPRINT_ASSET_EXTS = BLUEPRINT_IMAGE_EXTS | BLUEPRINT_FONT_EXTS
MAX_BUNDLE_BYTES = 20 * 1024 * 1024
MAX_BUNDLE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_BUNDLE_FILES = 1024
BUNDLE_ASSET_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".ttf", ".otf"}
BUNDLE_ALLOWED_EXTS = BUNDLE_ASSET_EXTS | {".yml", ".yaml"}
WINDOWS_RESERVED_STEMS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)),
}

FORBIDDEN_BLUEPRINT_SECTIONS = {
    "WebUI",
    "ApiPart",
    "PlexPart",
    "JellyfinPart",
    "EmbyPart",
    "SiloPart",
    "Notification",
    "SchedulingPart",
}

ALLOWED_BLUEPRINT_SECTIONS = {
    "OverlayPart",
    "PrerequisitePart",
    "PosterOverlayPart",
    "SeasonPosterOverlayPart",
    "ShowTitleOnSeasonPosterPart",
    "BackgroundOverlayPart",
    "TitleCardOverlayPart",
    "TitleCardTitleTextPart",
    "TitleCardEPTextPart",
    "CollectionPosterOverlayPart",
    "CollectionTitlePosterPart",
    "LogoPart",
}

ALLOWED_OVERLAY_PART_KEYS = {"ImageProcessing", "outputQuality"}

ALLOWED_PREREQUISITE_VISUAL_KEYS = {
    "SkipAddText",
    "SkipAddTextAndOverlay",
    "SkipAddTextAndBorder",
    "SkipLocalPosterTextAdd",
    "SkipLocalBackgroundTextAdd",
    "SkipLocalSeasonTextAdd",
    "SkipLocalTCTextAdd",
    "font",
    "collectionfont",
    "RTLFont",
    "backgroundfont",
    "titlecardfont",
    "overlayfile",
    "showoverlayfile",
    "seasonoverlayfile",
    "collectionoverlayfile",
    "backgroundoverlayfile",
    "showbackgroundoverlayfile",
    "titlecardoverlayfile",
    "poster4k",
    "Poster1080p",
    "Background4k",
    "Background1080p",
    "TC4k",
    "TC1080p",
    "4KDoVi",
    "4KHDR10",
    "4KDoViHDR10",
    "4KDoViBackground",
    "4KHDR10Background",
    "4KDoViHDR10Background",
    "4KDoViTC",
    "4KHDR10TC",
    "4KDoViHDR10TC",
    "UsePosterResolutionOverlays",
    "UseBackgroundResolutionOverlays",
    "UseTCResolutionOverlays",
    "Posters",
    "SeasonPosters",
    "BackgroundPosters",
    "TitleCards",
    "NewLineOnSpecificSymbols",
    "NewLineSymbols",
    "NewLineOnSpecificWords",
    "NewLineWords",
    "SymbolsToKeepOnNewLine",
    "AutoCreateSeasonTemplate",
    "AutoUpdateExistingSeasonPosters",
    "UseLogo",
    "UseBGLogo",
    "UseClearlogo",
    "UseClearart",
    "LogoTextFallback",
    "LogoExifCheck",
    "TextlessPosterBypass",
    "ConvertLogoColor",
    "LogoFlatColor",
    "PreserveMultiColorLogos",
    "UseOriginalTitle",
}

FILENAME_KEYS = {
    "font",
    "collectionfont",
    "RTLFont",
    "backgroundfont",
    "titlecardfont",
    "overlayfile",
    "showoverlayfile",
    "seasonoverlayfile",
    "collectionoverlayfile",
    "backgroundoverlayfile",
    "showbackgroundoverlayfile",
    "titlecardoverlayfile",
    "poster4k",
    "Poster1080p",
    "Background4k",
    "Background1080p",
    "TC4k",
    "TC1080p",
    "4KDoVi",
    "4KHDR10",
    "4KDoViHDR10",
    "4KDoViBackground",
    "4KHDR10Background",
    "4KDoViHDR10Background",
    "4KDoViTC",
    "4KHDR10TC",
    "4KDoViHDR10TC",
}

SECRET_PATTERNS = [
    ("Discord webhook URL", re.compile(r"https?://(?:ptb\.|canary\.)?(?:discord|discordapp)\.com/api/webhooks/", re.IGNORECASE)),
    ("Plex token query parameter", re.compile(r"X-Plex-Token\s*[:=]\s*[^\s&\"']+", re.IGNORECASE)),
    ("Private LAN IP URL", re.compile(r"https?://(?:192\.168\.|10\.|172\.(?:1[6-9]|2\d|3[01])\.|127\.0\.0\.1|localhost[:/])", re.IGNORECASE)),
]


def scan_text_for_secrets(text: str, context: str, errors: list[str]) -> None:
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            errors.append(f"{context}: contains potential sensitive value ({label}).")


def validate_safe_rel_filename(name: object) -> bool:
    if not isinstance(name, str) or not name or "/" in name or "\\" in name or ".." in name or ":" in name:
        return False
    return not any(ord(ch) < 32 for ch in name)


def validate_blueprint_asset_filename(name: object) -> bool:
    if not validate_safe_rel_filename(name):
        return False
    text = str(name)
    if text != text.strip() or text in {".", ".."} or text.endswith((".", " ")):
        return False
    if text.split(".", 1)[0].upper() in WINDOWS_RESERVED_STEMS:
        return False
    return Path(text).suffix.lower() in BLUEPRINT_ASSET_EXTS


def validate_asset_magic_bytes(filename: str, data: bytes) -> bool:
    if not data:
        return False
    ext = Path(filename).suffix.lower()
    if ext == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in {".jpg", ".jpeg"}:
        return data.startswith(b"\xff\xd8\xff")
    if ext == ".webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if ext == ".ttf":
        return len(data) >= 4 and data[:4] in (b"\x00\x01\x00\x00", b"true", b"typ1", b"ttcf")
    if ext == ".otf":
        return data.startswith(b"OTTO")
    if ext == ".woff":
        return data.startswith(b"wOFF")
    if ext == ".woff2":
        return data.startswith(b"wOF2")
    return False


def validate_bundle_zip_path(name: str) -> PurePosixPath | None:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or any(ord(ch) < 32 for ch in name):
        return None
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
        return None
    return path


def find_blueprint_file(entry_dir: Path, rel_path: str, errors: list[str]) -> Path | None:
    zip_candidates = sorted(p for p in entry_dir.iterdir() if p.is_file() and p.suffix.lower() == ".zip")
    json_candidates = sorted(
        p for p in entry_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".json" and p.name != "meta.json"
    )
    if zip_candidates and json_candidates:
        errors.append(f"{rel_path}: keep either custom_blueprint.zip or custom_blueprint.json, not both")
        return None
    for preferred in ("blueprint.zip", "custom_blueprint.zip", "blueprint.json", "custom_blueprint.json"):
        candidate = entry_dir / preferred
        if candidate.is_file():
            return candidate
    if len(zip_candidates) == 1:
        return zip_candidates[0]
    if len(zip_candidates) > 1:
        errors.append(f"{rel_path}: found multiple ZIP files; keep only custom_blueprint.zip or blueprint.zip")
        return None
    if len(json_candidates) == 1:
        return json_candidates[0]
    if len(json_candidates) > 1:
        errors.append(f"{rel_path}: found multiple JSON files; keep only custom_blueprint.json or blueprint.json")
    else:
        errors.append(f"{rel_path}: missing blueprint file (custom_blueprint.zip / custom_blueprint.json)")
    return None


def find_bundle_file(entry_dir: Path, rel_path: str, errors: list[str]) -> Path | None:
    for preferred in ("bundle.zip", "posterizarr-overlays.zip"):
        candidate = entry_dir / preferred
        if candidate.is_file():
            return candidate
    others = sorted(p for p in entry_dir.iterdir() if p.is_file() and p.suffix.lower() == ".zip")
    if len(others) == 1:
        return others[0]
    if len(others) > 1:
        errors.append(f"{rel_path}: found multiple ZIP files; keep only posterizarr-overlays.zip or bundle.zip")
    else:
        errors.append(f"{rel_path}: missing overlay ZIP file (e.g. posterizarr-overlays.zip or bundle.zip)")
    return None


def validate_meta(entry_dir: Path, rel_path: str, errors: list[str], default_author: str | None = None) -> dict | None:
    meta_path = entry_dir / "meta.json"
    if not meta_path.is_file():
        errors.append(f"{rel_path}: missing meta.json")
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{rel_path}: invalid meta.json ({exc})")
        return None
    if not isinstance(meta, dict):
        errors.append(f"{rel_path}: meta.json must be a JSON object")
        return None

    normalized = dict(meta)

    name = normalized.get("name")
    if not isinstance(name, str) or not (2 <= len(name.strip()) <= 80):
        errors.append(f"{rel_path}: meta.json 'name' must be a string of 2..80 characters")

    description = normalized.get("description")
    if not isinstance(description, str) or not (10 <= len(description.strip()) <= 400):
        errors.append(f"{rel_path}: meta.json 'description' must be a string of 10..400 characters")

    author = normalized.get("author")
    if author in (None, "") and default_author:
        normalized["author"] = default_author
    elif not isinstance(author, str) or not (1 <= len(author.strip()) <= 50):
        errors.append(f"{rel_path}: meta.json 'author' must be a string of 1..50 characters")

    tags = normalized.get("tags")
    if tags is None:
        normalized["tags"] = []
    elif not isinstance(tags, list) or len(tags) > 8 or not all(isinstance(t, str) and TAG_RE.fullmatch(t) for t in tags):
        errors.append(f"{rel_path}: meta.json 'tags' must contain up to 8 lowercase tags matching {TAG_RE.pattern}")

    previews = normalized.get("previews")
    if previews is None or previews == []:
        discovered = sorted(
            p.name for p in entry_dir.iterdir()
            if p.is_file() and p.suffix.lower() in PREVIEW_EXTS
        )
        normalized["previews"] = discovered[:4]
        previews = normalized["previews"]

    if not isinstance(previews, list) or not (1 <= len(previews) <= 4):
        errors.append(f"{rel_path}: add 1..4 preview images (.png, .jpg, .jpeg, .webp) or list them in meta.json 'previews'")
    else:
        for preview in previews:
            if not validate_safe_rel_filename(preview) or Path(preview).suffix.lower() not in PREVIEW_EXTS:
                errors.append(f"{rel_path}: invalid preview filename '{preview}'")
                continue
            p_file = entry_dir / preview
            if not p_file.is_file():
                errors.append(f"{rel_path}: preview file '{preview}' does not exist")
            elif p_file.stat().st_size == 0 or p_file.stat().st_size > MAX_PREVIEW_BYTES:
                errors.append(f"{rel_path}: preview file '{preview}' must be between 1 byte and {MAX_PREVIEW_BYTES} bytes")

    if normalized.get("author_url") is None and isinstance(normalized.get("author"), str) and AUTHOR_RE.fullmatch(normalized["author"].strip()):
        normalized["author_url"] = f"https://github.com/{normalized['author'].strip()}"

    for url_key in ("author_url", "source_url"):
        val = normalized.get(url_key)
        if val is not None and (not isinstance(val, str) or (val != "" and not val.startswith("https://"))):
            errors.append(f"{rel_path}: meta.json '{url_key}' must start with https://")

    notes = normalized.get("notes")
    if notes is not None and (not isinstance(notes, str) or len(notes) > 500):
        errors.append(f"{rel_path}: meta.json 'notes' must be a string up to 500 characters")

    min_ver = normalized.get("min_posterizarr_version")
    if min_ver is not None and (not isinstance(min_ver, str) or len(min_ver.strip()) > 32):
        errors.append(f"{rel_path}: meta.json 'min_posterizarr_version' must be a short string")

    return normalized


def build_release_asset_name(kind: str, entry_id: str, suffix: str) -> str:
    """Build a deterministic, collision-free, GitHub-Release-safe asset filename."""
    ext = suffix.lower()
    if ext not in {".json", ".zip"}:
        raise ValueError(f"Unsupported release asset suffix '{suffix}' for {kind}:{entry_id}")
    raw_slug = entry_id.replace("/", "--").lower()
    slug = SAFE_RELEASE_SEGMENT_RE.sub("-", raw_slug)
    slug = re.sub(r"\.{2,}", ".", slug).strip(".-_")[:80] or "entry"
    digest8 = hashlib.sha256(f"{kind}:{entry_id}".encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest8}{ext}"


def validate_blueprint_entry(entry_id: str, rel_path: str, entry_dir: Path, errors: list[str], default_author: str | None = None) -> dict | None:
    meta = validate_meta(entry_dir, rel_path, errors, default_author=default_author)
    bp_path = find_blueprint_file(entry_dir, rel_path, errors)
    if bp_path is None:
        return None
    raw_bytes = bp_path.read_bytes()
    is_zip = bp_path.suffix.lower() == ".zip"
    bundled_assets: dict[str, bytes] = {}

    if is_zip:
        if len(raw_bytes) == 0 or len(raw_bytes) > MAX_BLUEPRINT_BUNDLE_BYTES:
            errors.append(f"{rel_path}: {bp_path.name} must be 1..{MAX_BLUEPRINT_BUNDLE_BYTES} bytes")
            return None
        blueprint_raw: bytes | None = None
        seen_paths: set[str] = set()
        try:
            with zipfile.ZipFile(bp_path) as archive:
                members = archive.infolist()
                if len(members) > MAX_BLUEPRINT_BUNDLE_FILES or sum(m.file_size for m in members) > MAX_BLUEPRINT_UNCOMPRESSED_BYTES:
                    errors.append(f"{rel_path}: {bp_path.name} exceeds file count or uncompressed size limit")
                    return None
                for member in members:
                    if member.is_dir():
                        continue
                    path = validate_bundle_zip_path(member.orig_filename)
                    if path is None:
                        errors.append(f"{rel_path}: {bp_path.name} contains invalid path '{member.orig_filename}'")
                        return None
                    if stat.S_ISLNK(member.external_attr >> 16) or (member.flag_bits & 1):
                        errors.append(f"{rel_path}: {bp_path.name} contains symlink or encrypted entry '{path}'")
                        return None
                    folded = path.as_posix().casefold()
                    if folded in seen_paths:
                        errors.append(f"{rel_path}: {bp_path.name} contains duplicate path '{path}'")
                        return None
                    seen_paths.add(folded)
                    if member.file_size > 1024 * 1024 and member.file_size > max(1, member.compress_size) * 200:
                        errors.append(f"{rel_path}: {bp_path.name} contains excessive compression in '{path}'")
                        return None
                    if path.as_posix() == "blueprint.json":
                        if member.file_size == 0 or member.file_size > MAX_BLUEPRINT_BYTES:
                            errors.append(f"{rel_path}: bundled blueprint.json must be 1..{MAX_BLUEPRINT_BYTES} bytes")
                            return None
                        blueprint_raw = archive.read(member)
                    elif len(path.parts) == 2 and path.parts[0] == "assets" and validate_blueprint_asset_filename(path.parts[1]):
                        asset_name = path.parts[1]
                        if member.file_size == 0 or member.file_size > MAX_BLUEPRINT_ASSET_BYTES:
                            errors.append(f"{rel_path}: bundled asset '{asset_name}' exceeds {MAX_BLUEPRINT_ASSET_BYTES} bytes")
                            return None
                        data = archive.read(member)
                        if not validate_asset_magic_bytes(asset_name, data):
                            errors.append(f"{rel_path}: bundled asset '{asset_name}' failed format verification")
                            return None
                        bundled_assets[asset_name] = data
                    else:
                        errors.append(f"{rel_path}: {bp_path.name} contains disallowed file '{path}'")
                        return None
        except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
            errors.append(f"{rel_path}: invalid {bp_path.name} ({exc})")
            return None

        if blueprint_raw is None:
            errors.append(f"{rel_path}: {bp_path.name} missing blueprint.json")
            return None
        try:
            raw_text = blueprint_raw.decode("utf-8")
            bp_json = json.loads(raw_text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"{rel_path}: invalid bundled blueprint.json ({exc})")
            return None
        if not isinstance(bp_json, dict):
            errors.append(f"{rel_path}: bundled blueprint.json must be a JSON object")
            return None
        if bp_json.get("kind") != BLUEPRINT_BUNDLE_KIND or bp_json.get("version") != BLUEPRINT_BUNDLE_VERSION:
            errors.append(f"{rel_path}: unsupported blueprint bundle manifest kind/version")
            return None
        manifest_assets = bp_json.get("assets")
        if not isinstance(manifest_assets, dict) or set(manifest_assets.keys()) != set(bundled_assets.keys()):
            errors.append(f"{rel_path}: blueprint bundle asset manifest mismatch")
            return None
        for asset_name, data in bundled_assets.items():
            asset_meta = manifest_assets.get(asset_name)
            if not isinstance(asset_meta, dict) or asset_meta.get("sha256") != hashlib.sha256(data).hexdigest():
                errors.append(f"{rel_path}: checksum mismatch for bundled asset '{asset_name}'")
    else:
        if len(raw_bytes) == 0 or len(raw_bytes) > MAX_BLUEPRINT_BYTES:
            errors.append(f"{rel_path}: blueprint.json must be 1..{MAX_BLUEPRINT_BYTES} bytes")
            return None
        try:
            raw_text = raw_bytes.decode("utf-8")
            bp_json = json.loads(raw_text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"{rel_path}: invalid blueprint.json ({exc})")
            return None
        if not isinstance(bp_json, dict):
            errors.append(f"{rel_path}: blueprint.json must be a JSON object")
            return None

    scan_text_for_secrets(raw_text, f"{rel_path}/blueprint.json", errors)

    updates = bp_json.get("updates")
    if isinstance(updates, dict) and isinstance(updates.get("nested"), dict):
        sections = updates["nested"]
    else:
        sections = {k: v for k, v in bp_json.items() if k != "name" and isinstance(v, dict)}

    if not isinstance(sections, dict) or not sections:
        errors.append(f"{rel_path}: blueprint.json contains no configuration sections")
        return None

    settings_count = 0
    referenced_folded: set[str] = set()
    for section_name, section_fields in sections.items():
        if section_name in FORBIDDEN_BLUEPRINT_SECTIONS or section_name not in ALLOWED_BLUEPRINT_SECTIONS:
            errors.append(f"{rel_path}: disallowed blueprint section '{section_name}'")
            continue
        if not isinstance(section_fields, dict):
            errors.append(f"{rel_path}: section '{section_name}' must be an object")
            continue
        for key, value in section_fields.items():
            if section_name == "OverlayPart" and key not in ALLOWED_OVERLAY_PART_KEYS:
                errors.append(f"{rel_path}: disallowed key '{section_name}.{key}'")
            elif section_name == "PrerequisitePart" and key not in ALLOWED_PREREQUISITE_VISUAL_KEYS:
                errors.append(f"{rel_path}: disallowed key 'PrerequisitePart.{key}'")
            elif key in FILENAME_KEYS and value not in (None, ""):
                text_val = str(value)
                if not validate_safe_rel_filename(text_val):
                    errors.append(f"{rel_path}: filename key '{section_name}.{key}' must not contain path separators")
                else:
                    referenced_folded.add(text_val.strip().casefold())
            settings_count += 1

    if is_zip:
        for asset_name in bundled_assets:
            if asset_name.casefold() not in referenced_folded:
                errors.append(f"{rel_path}: bundled asset '{asset_name}' is not referenced in blueprint.json")

    if meta is None or any(err.startswith(f"{rel_path}:") for err in errors):
        return None

    prereq = sections.get("PrerequisitePart") or {}
    poster = sections.get("PosterOverlayPart") or {}
    uses_logo = str(prereq.get("UseLogo", "")).lower() == "true" or str(prereq.get("UseClearlogo", "")).lower() == "true" or str(prereq.get("UseClearart", "")).lower() == "true"
    uses_borders = str(poster.get("AddBorder", "")).lower() == "true"
    uses_text = str(poster.get("AddText", "")).lower() == "true"

    result = {
        "id": entry_id,
        "kind": "blueprint",
        "name": meta["name"].strip(),
        "description": meta["description"].strip(),
        "author": meta["author"].strip(),
        "author_url": meta.get("author_url") or None,
        "source_url": meta.get("source_url") or None,
        "tags": meta["tags"],
        "min_posterizarr_version": meta.get("min_posterizarr_version") or "4.0.0",
        "notes": meta.get("notes") or None,
        "path": rel_path,
        "file": f"{rel_path}/{bp_path.name}",
        "previews": [f"{rel_path}/{p}" for p in meta["previews"]],
        "sections_count": len(sections),
        "settings_count": settings_count,
        "uses_logo": uses_logo,
        "uses_borders": uses_borders,
        "uses_text": uses_text,
        "is_bundle": is_zip,
        "asset_count": len(bundled_assets),
        "file_size_bytes": len(raw_bytes),
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "release_tag": BLUEPRINTS_RELEASE_TAG,
        "release_asset": build_release_asset_name("blueprint", entry_id, bp_path.suffix),
    }
    if is_zip:
        result["assets"] = sorted(bundled_assets)
    return result


def validate_overlay_entry(entry_id: str, rel_path: str, entry_dir: Path, errors: list[str], default_author: str | None = None) -> dict | None:
    meta = validate_meta(entry_dir, rel_path, errors, default_author=default_author)
    zip_path = find_bundle_file(entry_dir, rel_path, errors)
    if zip_path is None:
        return None

    raw_bytes = zip_path.read_bytes()
    if len(raw_bytes) == 0 or len(raw_bytes) > MAX_BUNDLE_BYTES:
        errors.append(f"{rel_path}: bundle.zip must be 1..{MAX_BUNDLE_BYTES} bytes")
        return None

    files: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = archive.infolist()
            if len(members) > MAX_BUNDLE_FILES or sum(m.file_size for m in members) > MAX_BUNDLE_UNCOMPRESSED_BYTES:
                errors.append(f"{rel_path}: bundle.zip exceeds file count or uncompressed size limit")
                return None
            seen_paths: set[str] = set()
            for member in members:
                if member.is_dir():
                    continue
                path = validate_bundle_zip_path(member.orig_filename)
                if path is None:
                    errors.append(f"{rel_path}: bundle.zip contains invalid path '{member.orig_filename}'")
                    return None
                if stat.S_ISLNK(member.external_attr >> 16) or (member.flag_bits & 1):
                    errors.append(f"{rel_path}: bundle.zip contains symlink or encrypted entry '{path}'")
                    return None
                folded = path.as_posix().casefold()
                if folded in seen_paths:
                    errors.append(f"{rel_path}: bundle.zip contains duplicate path '{path}'")
                    return None
                seen_paths.add(folded)
                if path.as_posix() != "manifest.json" and not (
                    path.parts[0] == "files" and path.suffix.lower() in BUNDLE_ALLOWED_EXTS
                ):
                    errors.append(f"{rel_path}: bundle.zip contains disallowed file '{path}'")
                    return None
                files[path.as_posix()] = archive.read(member)
    except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        errors.append(f"{rel_path}: invalid bundle.zip ({exc})")
        return None

    if "manifest.json" not in files:
        errors.append(f"{rel_path}: bundle.zip missing manifest.json")
        return None
    if "files/configs/config.yml" not in files:
        errors.append(f"{rel_path}: bundle.zip missing files/configs/config.yml")
        return None

    try:
        manifest_text = files.pop("manifest.json").decode("utf-8")
        manifest = json.loads(manifest_text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{rel_path}: bundle.zip has invalid manifest.json ({exc})")
        return None

    if not isinstance(manifest, dict) or manifest.get("kind") != "posterizarr-overlay-bundle" or manifest.get("version") != 1:
        errors.append(f"{rel_path}: unsupported bundle manifest kind/version")
        return None

    scan_text_for_secrets(manifest_text, f"{rel_path}/bundle.zip:manifest.json", errors)

    checksums = manifest.get("checksums") or {}
    if not isinstance(checksums, dict):
        errors.append(f"{rel_path}: manifest.json checksums must be an object")
        return None

    for fname, payload in files.items():
        expected = checksums.get(fname)
        actual = hashlib.sha256(payload).hexdigest()
        if expected != actual:
            errors.append(f"{rel_path}: checksum mismatch for '{fname}'")
        if fname.lower().endswith((".yml", ".yaml")):
            try:
                scan_text_for_secrets(payload.decode("utf-8"), f"{rel_path}/bundle.zip:{fname}", errors)
            except UnicodeDecodeError:
                errors.append(f"{rel_path}: YAML file '{fname}' is not valid UTF-8")

    libraries = manifest.get("libraries")
    if not isinstance(libraries, list) or not libraries or not all(
        isinstance(lib, dict) and isinstance(lib.get("name"), str) and lib["name"] for lib in libraries
    ):
        errors.append(f"{rel_path}: manifest.json must include a non-empty list of libraries")
        return None

    if len({lib["name"] for lib in libraries}) != len(libraries):
        errors.append(f"{rel_path}: manifest.json has duplicate library names")
        return None

    if meta is None or any(err.startswith(f"{rel_path}:") for err in errors):
        return None

    library_summaries = [
        {
            "name": lib["name"],
            "media_type": lib.get("media_type") or "mixed",
            "rule_count": int(lib.get("rule_count") or 0),
            "animetafill": bool(lib.get("animetafill")),
            "season_ratings": bool(lib.get("season_ratings")),
        }
        for lib in libraries
    ]
    library_types = sorted({item["media_type"] for item in library_summaries if item["media_type"]})
    settings = manifest.get("settings") or {}
    animetafill = manifest.get("animetafill") or {}
    dependencies = manifest.get("official_dependencies") or {}

    return {
        "id": entry_id,
        "kind": "dynamic-overlay",
        "name": meta["name"].strip(),
        "description": meta["description"].strip(),
        "author": meta["author"].strip(),
        "author_url": meta.get("author_url") or None,
        "source_url": meta.get("source_url") or None,
        "tags": meta["tags"],
        "min_posterizarr_version": meta.get("min_posterizarr_version") or "4.0.0",
        "notes": meta.get("notes") or None,
        "path": rel_path,
        "file": f"{rel_path}/{zip_path.name}",
        "previews": [f"{rel_path}/{p}" for p in meta["previews"]],
        "libraries": library_summaries,
        "library_types": library_types,
        "features": {
            "animetafill": bool(animetafill.get("enabled")),
            "imdb_dataset": bool(manifest.get("imdb_dataset")),
            "rating_sources": list(settings.get("rating_sources") or []),
            "official_dependency_count": len(dependencies) if isinstance(dependencies, dict) else 0,
        },
        "file_count": len(files),
        "asset_count": sum(PurePosixPath(name).suffix.lower() in BUNDLE_ASSET_EXTS for name in files),
        "bundle_size_bytes": len(raw_bytes),
        "bundle_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "release_tag": OVERLAYS_RELEASE_TAG,
        "release_asset": build_release_asset_name("dynamic-overlay", entry_id, zip_path.suffix),
    }


def collect_entries(base_dir: Path, validator, errors: list[str]) -> list[dict]:
    items: list[dict] = []
    if not base_dir.exists():
        return items
    for author_path in sorted(base_dir.iterdir(), key=lambda p: p.name.lower()):
        if not author_path.is_dir():
            errors.append(f"{base_dir.name}/{author_path.name}: unexpected file at author level")
            continue
        if not AUTHOR_RE.fullmatch(author_path.name):
            errors.append(f"{base_dir.name}/{author_path.name}: invalid author directory name")
            continue

        is_flat_entry = (author_path / "meta.json").is_file() or any(
            p.is_file() and p.suffix.lower() in {".json", ".zip"}
            for p in author_path.iterdir()
        )
        if is_flat_entry:
            rel_path = f"{base_dir.name}/{author_path.name}"
            item = validator(author_path.name, rel_path, author_path, errors, default_author=author_path.name)
            if item is not None:
                items.append(item)
            continue

        for slug_path in sorted(author_path.iterdir(), key=lambda p: p.name.lower()):
            if not slug_path.is_dir():
                errors.append(f"{base_dir.name}/{author_path.name}/{slug_path.name}: unexpected file at slug level")
                continue
            if not SLUG_RE.fullmatch(slug_path.name):
                errors.append(f"{base_dir.name}/{author_path.name}/{slug_path.name}: invalid entry slug")
                continue
            entry_id = f"{author_path.name}/{slug_path.name}"
            rel_path = f"{base_dir.name}/{entry_id}"
            item = validator(entry_id, rel_path, slug_path, errors, default_author=author_path.name)
            if item is not None:
                items.append(item)
    return items


def build_index() -> tuple[dict, list[str]]:
    errors: list[str] = []
    blueprints = collect_entries(BLUEPRINTS_DIR, validate_blueprint_entry, errors)
    dynamic_overlays = collect_entries(OVERLAYS_DIR, validate_overlay_entry, errors)

    for category_label, items in (("blueprints", blueprints), ("dynamic_overlays", dynamic_overlays)):
        if len(items) > MAX_RELEASE_ASSETS:
            errors.append(
                f"{category_label}: {len(items)} entries exceeds the {MAX_RELEASE_ASSETS}-asset limit per GitHub Release."
            )

    seen_assets: dict[tuple[str, str], str] = {}
    for item in (*blueprints, *dynamic_overlays):
        r_tag = item.get("release_tag")
        r_asset = item.get("release_asset")
        if isinstance(r_tag, str) and isinstance(r_asset, str):
            key = (r_tag, r_asset.casefold())
            prev_id = seen_assets.get(key)
            if prev_id is not None:
                errors.append(
                    f"{item.get('path')}: release asset name '{r_asset}' collides with '{prev_id}' in '{r_tag}'."
                )
            else:
                seen_assets[key] = str(item.get("id"))

    index = {
        "schema_version": 1,
        "repo": REPO_SLUG,
        "branch": REPO_BRANCH,
        "blueprints": blueprints,
        "dynamic_overlays": dynamic_overlays,
    }
    return index, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Posterizarr community presets and generate index.json.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if any validation error occurs.")
    parser.add_argument("--write-index", action="store_true", help="Write generated index.json when valid.")
    args = parser.parse_args()

    index, errors = build_index()
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        if args.strict:
            return 1

    print(
        f"Validated {len(index['blueprints'])} blueprint(s) and {len(index['dynamic_overlays'])} dynamic-overlay bundle(s)."
    )

    if args.write_index:
        if errors:
            print("Refusing to write index.json while validation errors exist.", file=sys.stderr)
            return 1
        rendered = json.dumps(index, indent=2, ensure_ascii=False) + "\n"
        if not INDEX_PATH.exists() or INDEX_PATH.read_text(encoding="utf-8") != rendered:
            INDEX_PATH.write_text(rendered, encoding="utf-8")
            print(f"Wrote {INDEX_PATH}")
        else:
            print("index.json is already up to date.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
