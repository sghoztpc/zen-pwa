#!/usr/bin/env python3
"""Zen PWA Native Messaging Host for Windows.

Manages isolated Zen Browser standalone app profiles, .ico icons,
domain-scoped login cookie sync, and Windows Start Menu / Desktop shortcuts.
"""

from __future__ import annotations

import base64
import configparser
import ctypes
import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from uuid import UUID

from PIL import Image, ImageDraw, ImageFont

VERSION = "1.3.0"
SELF_EXTENSION_ID = "zen-pwa-ct2026@lazymoon.dev"
MAX_MESSAGE_BYTES = 1_048_576
MAX_ICON_BYTES = 5 * 1024 * 1024
MAX_ICON_DIMENSION = 4096
MAX_REDIRECTS = 3
Image.MAX_IMAGE_PIXELS = 16_000_000

APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Ecosystem sibling domains used during SSO sign-in so apps like YouTube Music stay signed in.
SSO_ECOSYSTEMS: list[set[str]] = [
    {"youtube.com", "youtu.be", "google.com"},
    {"microsoft.com", "live.com", "office.com", "outlook.com", "microsoftonline.com"},
    {"apple.com", "icloud.com"},
]

# Multi-tenant hosting suffixes where each subdomain is an independent site.
SHARED_HOSTING_SUFFIXES = {
    "github.io", "gitlab.io", "pages.dev", "vercel.app", "netlify.app",
    "herokuapp.com", "appspot.com", "web.app", "firebaseapp.com",
    "blogspot.com", "wordpress.com", "azurewebsites.net", "cloudfront.net",
    "amazonaws.com", "ondigitalocean.app", "fly.dev", "railway.app", "render.com",
}

STANDARD_GTLDS = {
    "com", "org", "net", "io", "app", "dev", "ai", "tv", "me", "co", "gg",
    "fm", "so", "sh", "xyz", "info", "biz", "edu", "gov", "music", "chat",
}

DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ZenPWA"
PROFILES_DIR = DATA_ROOT / "profiles"
ICONS_DIR = DATA_ROOT / "icons"
LAUNCHERS_DIR = DATA_ROOT / "launchers"
APPS_DB_PATH = DATA_ROOT / "apps.json"

CSC_EXE = (
    Path(os.environ.get("SystemRoot", r"C:\Windows"))
    / "Microsoft.NET"
    / "Framework64"
    / "v4.0.30319"
    / "csc.exe"
)

ZEN_APPDATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "zen"

START_MENU_PROGRAMS_DIR = (
    Path(os.environ.get("APPDATA", str(Path.home())))
    / "Microsoft"
    / "Windows"
    / "Start Menu"
    / "Programs"
)
START_MENU_DIR = START_MENU_PROGRAMS_DIR / "Zen Apps"

POWERSHELL_EXE = (
    Path(os.environ.get("SystemRoot", r"C:\Windows"))
    / "System32"
    / "WindowsPowerShell"
    / "v1.0"
    / "powershell.exe"
)


def get_windows_desktop_dir() -> Path:
    """Resolve the real Windows Desktop folder (handles OneDrive redirection)."""
    try:
        folder_id = UUID("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}")
        path_ptr = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(ctypes.create_string_buffer(folder_id.bytes_le, 16)),
            0,
            None,
            ctypes.byref(path_ptr),
        )
        if hr == 0 and path_ptr.value:
            result = Path(path_ptr.value)
            ctypes.windll.ole32.CoTaskMemFree(path_ptr)
            return result
    except Exception:
        pass
    return Path.home() / "Desktop"


DESKTOP_DIR = get_windows_desktop_dir()


def find_zen_exe() -> Path:
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Zen Browser" / "zen.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Zen Browser" / "zen.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Zen Browser" / "zen.exe",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("Zen Browser executable (zen.exe) was not found.")


def find_default_zen_profile() -> Path | None:
    """Locate the user's active default Zen Browser profile from profiles.ini."""
    ini_path = ZEN_APPDATA_DIR / "profiles.ini"
    if not ini_path.is_file():
        return None
    parser = configparser.RawConfigParser()
    try:
        parser.read(ini_path, encoding="utf-8")
    except (OSError, UnicodeDecodeError, configparser.Error):
        return None

    zen_root = ZEN_APPDATA_DIR.resolve()

    def resolve_safe(raw_path: str, is_relative: bool) -> Path | None:
        candidate = (zen_root / raw_path if is_relative else Path(raw_path)).resolve()
        if is_relative and not candidate.is_relative_to(zen_root):
            return None
        if candidate.is_dir() and (candidate / "cookies.sqlite").is_file():
            return candidate
        return None

    for section in parser.sections():
        if section.startswith("Install") and parser.has_option(section, "Default"):
            found = resolve_safe(parser.get(section, "Default").strip(), True)
            if found:
                return found

    for section in parser.sections():
        if section.startswith("Profile") and parser.get(section, "Default", fallback="0").strip() == "1":
            raw_path = parser.get(section, "Path", fallback="").strip()
            is_rel = parser.get(section, "IsRelative", fallback="1").strip() == "1"
            found = resolve_safe(raw_path, is_rel)
            if found:
                return found
    return None


def get_base_domain(hostname: str) -> str:
    host = hostname.lower().strip(".")
    try:
        ipaddress.ip_address(host.strip("[]"))
        return host
    except ValueError:
        pass

    parts = host.split(".")
    if len(parts) <= 2:
        return host
    last_two = ".".join(parts[-2:])
    if last_two in SHARED_HOSTING_SUFFIXES:
        return host
    if parts[-1] in STANDARD_GTLDS:
        return last_two
    # For country-code TLDs or unrecognized suffixes, stay exact to avoid cross-site leakage.
    return host


def get_sync_domains(hostname: str) -> set[str]:
    base = get_base_domain(hostname)
    domains = {base}
    for group in SSO_ECOSYSTEMS:
        if base in group:
            domains.update(group)
    return domains


def host_matches_domains(cookie_host: str, allowed_domains: set[str]) -> bool:
    h = (cookie_host or "").lstrip(".").lower()
    return any(h == d or h.endswith("." + d) for d in allowed_domains)


def read_source_cookies(src_db: Path, allowed_domains: set[str]) -> tuple[str, list[str], int, list[str], list[tuple[Any, ...]]]:
    conn = sqlite3.connect(f"{src_db.as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        schema_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='moz_cookies'"
        ).fetchone()
        if not schema_row or not schema_row[0]:
            raise ValueError("Could not read moz_cookies schema.")
        create_sql = schema_row[0]
        index_sqls = [
            r[0]
            for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='moz_cookies' AND sql IS NOT NULL"
            ).fetchall()
        ]
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        cols = [r[1] for r in conn.execute("PRAGMA table_info(moz_cookies)").fetchall()]
        col_list = ", ".join(f'"{c}"' for c in cols)
        host_idx = cols.index("host")
        where_clause = " WHERE originAttributes = ''" if "originAttributes" in cols else ""
        rows = [
            row
            for row in conn.execute(f"SELECT {col_list} FROM moz_cookies{where_clause}").fetchall()
            if host_matches_domains(str(row[host_idx] or ""), allowed_domains)
        ]
        return (create_sql, index_sqls, user_version, cols, rows)
    finally:
        conn.close()


def write_dest_cookies(
    dst_db: Path,
    allowed_domains: set[str],
    create_sql: str,
    index_sqls: list[str],
    user_version: int,
    cols: list[str],
    rows: list[tuple[Any, ...]],
) -> int:
    conn = sqlite3.connect(str(dst_db), timeout=5.0)
    try:
        conn.execute(f"PRAGMA user_version = {user_version}")
        conn.execute(create_sql.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
        for idx_sql in index_sqls:
            safe_idx = idx_sql.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1).replace(
                "CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ", 1
            )
            conn.execute(safe_idx)

        existing_hosts = [r[0] for r in conn.execute("SELECT DISTINCT host FROM moz_cookies").fetchall()]
        for h in existing_hosts:
            if host_matches_domains(str(h or ""), allowed_domains):
                conn.execute("DELETE FROM moz_cookies WHERE host = ?", (h,))

        insert_cols = [c for c in cols if c != "id"]
        insert_indices = [cols.index(c) for c in insert_cols]
        placeholders = ", ".join("?" for _ in insert_cols)
        insert_col_sql = ", ".join(f'"{c}"' for c in insert_cols)
        filtered_rows = [tuple(r[i] for i in insert_indices) for r in rows]
        conn.executemany(
            f"INSERT OR REPLACE INTO moz_cookies ({insert_col_sql}) VALUES ({placeholders})",
            filtered_rows,
        )
        conn.commit()
        return len(filtered_rows)
    finally:
        conn.close()


def sync_domain_cookies(profile_dir: Path, start_url: str) -> tuple[int, str | None]:
    """Copy default-container, domain-scoped cookies from main Zen into the app profile."""
    src_profile = find_default_zen_profile()
    if not src_profile:
        return (0, "Default Zen profile not found; skipped login sync.")

    src_db = src_profile / "cookies.sqlite"
    hostname = urllib.parse.urlparse(start_url).hostname or ""
    if not src_db.is_file() or not hostname:
        return (0, None)

    allowed_domains = get_sync_domains(hostname)
    try:
        create_sql, index_sqls, user_version, cols, rows = read_source_cookies(src_db, allowed_domains)
    except Exception:
        return (0, "Could not read cookies from main Zen profile; skipped login sync.")

    if not rows:
        return (0, None)

    try:
        synced = write_dest_cookies(
            profile_dir / "cookies.sqlite",
            allowed_domains,
            create_sql,
            index_sqls,
            user_version,
            cols,
            rows,
        )
        return (synced, None)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            return (0, "App is currently open so login cookies could not be updated; close the app and reinstall to sync.")
        return (0, "Could not write cookies into app profile; skipped login sync.")
    except Exception:
        return (0, "Skipped login cookie sync due to an unexpected database error.")


MAX_EXTENSIONS_JSON_BYTES = 5 * 1024 * 1024
MAX_PREFS_JS_BYTES = 10 * 1024 * 1024
MAX_EXT_STORAGE_BYTES = 100 * 1024 * 1024
MAX_EXT_STORAGE_FILES = 2000
UUID_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
WEBEXT_UUIDS_LINE_RE = re.compile(r'^user_pref\("extensions\.webextensions\.uuids",\s*("(?:\\.|[^"\\])*")\);\s*$', re.MULTILINE)

# Never copy local/sync storage of password managers, crypto wallets, or 2FA vaults across profiles.
SENSITIVE_STORAGE_EXTENSION_IDS = {
    "{446900e4-71c2-419f-a6a7-df9c091e268b}",  # Bitwarden
    "{d634138d-c276-4fc8-924b-40a0ea21d284}",  # 1Password
    "78272b6fa58f4a1abaac99321d503a20@proton.me",  # Proton Pass
    "keepassxc-browser@keepassxc.org",  # KeePassXC-Browser
    "support@lastpass.com",  # LastPass
    "jetpack@dashlane.com",  # Dashlane
    "webextension@metamask.io",  # MetaMask
    "{a4c4eda4-fb84-4a84-b4a1-f7c1cbf2a1ad}",  # Phantom
    "rabby@debank.com",  # Rabby Wallet
    "authenticator@mymindstorm",  # Authenticator 2FA
}


def _is_link_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junc = getattr(path, "is_junction", None)
        if callable(is_junc) and is_junc():
            return True
        st = os.lstat(path)
        return bool(getattr(st, "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def _validate_xpi_candidate(candidate: Path, src_ext_dir: Path) -> Path | None:
    try:
        if _is_link_or_junction(candidate):
            return None
        resolved = candidate.resolve()
        if (
            resolved.is_file()
            and resolved.suffix.lower() == ".xpi"
            and resolved.is_relative_to(src_ext_dir)
            and resolved.stem.lower() != SELF_EXTENSION_ID.lower()
        ):
            return resolved
    except (OSError, ValueError):
        return None
    return None


def collect_zen_extension_xpis(src_profile: Path) -> list[Path]:
    """Discover active, non-symlinked .xpi files in the main Zen profile."""
    src_profile_res = src_profile.resolve()
    src_ext_raw = src_profile_res / "extensions"
    if _is_link_or_junction(src_ext_raw):
        return []
    try:
        src_ext_dir = src_ext_raw.resolve()
    except OSError:
        return []
    if not src_ext_dir.is_dir() or not src_ext_dir.is_relative_to(src_profile_res):
        return []

    seen: set[str] = set()
    candidates: list[Path] = []

    def add_unique(path: Path | None) -> None:
        if path is None:
            return
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            candidates.append(path)

    ext_json = src_profile_res / "extensions.json"
    used_manifest = False
    if ext_json.is_file() and not _is_link_or_junction(ext_json):
        try:
            if ext_json.stat().st_size <= MAX_EXTENSIONS_JSON_BYTES:
                data = json.loads(ext_json.read_text(encoding="utf-8"))
                addons = data.get("addons") if isinstance(data, dict) else None
                if isinstance(addons, list):
                    used_manifest = True
                    for addon in addons:
                        if not isinstance(addon, dict):
                            continue
                        if addon.get("type") != "extension" or addon.get("location") != "app-profile":
                            continue
                        if not addon.get("active") or addon.get("userDisabled"):
                            continue
                        addon_id = addon.get("id")
                        if not isinstance(addon_id, str) or not addon_id or addon_id.lower() == SELF_EXTENSION_ID.lower():
                            continue
                        raw_path = addon.get("path")
                        cand = Path(raw_path) if isinstance(raw_path, str) and raw_path else (src_ext_dir / f"{addon_id}.xpi")
                        add_unique(_validate_xpi_candidate(cand, src_ext_dir))
        except (OSError, UnicodeDecodeError, ValueError):
            used_manifest = False
            seen.clear()
            candidates.clear()

    if not used_manifest:
        for item in src_ext_dir.glob("*.xpi"):
            add_unique(_validate_xpi_candidate(item, src_ext_dir))

    return candidates


def _parse_webext_uuids_text(text: str) -> dict[str, str]:
    m = WEBEXT_UUIDS_LINE_RE.search(text)
    if not m:
        return {}
    try:
        inner_json = json.loads(m.group(1))
        mapping = json.loads(inner_json)
    except (ValueError, TypeError):
        return {}
    if not isinstance(mapping, dict):
        return {}
    return {
        k: v
        for k, v in mapping.items()
        if isinstance(k, str) and isinstance(v, str) and UUID_RE.fullmatch(v)
    }


def read_webext_uuids(prefs_path: Path) -> dict[str, str]:
    """Read and validate the extensions.webextensions.uuids map from a Firefox/Zen prefs.js."""
    if not prefs_path.is_file() or _is_link_or_junction(prefs_path):
        return {}
    try:
        if prefs_path.stat().st_size > MAX_PREFS_JS_BYTES:
            return {}
        text = prefs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return _parse_webext_uuids_text(text)


def merge_webext_uuids_into_prefs(profile_dir: Path, updates: dict[str, str]) -> bool:
    """Merge extension UUIDs into the app profile's prefs.js atomically."""
    raw_prefs = profile_dir / "prefs.js"
    if not updates or _is_link_or_junction(raw_prefs):
        return False
    try:
        prefs_path = safe_child(profile_dir, "prefs.js")
    except ValueError:
        return False
    existing_text = ""
    if prefs_path.is_file():
        try:
            if prefs_path.stat().st_size > MAX_PREFS_JS_BYTES:
                return False
            existing_text = prefs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

    merged = _parse_webext_uuids_text(existing_text)
    merged.update(updates)
    encoded_val = json.dumps(json.dumps(merged, separators=(",", ":")))
    new_line = f'user_pref("extensions.webextensions.uuids", {encoded_val});'

    if WEBEXT_UUIDS_LINE_RE.search(existing_text):
        updated_text = WEBEXT_UUIDS_LINE_RE.sub(lambda _: new_line, existing_text, count=1)
    else:
        sep = "" if (not existing_text or existing_text.endswith("\n")) else "\n"
        updated_text = f"{existing_text}{sep}{new_line}\n"

    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=prefs_path.parent, prefix=".prefs-", suffix=".tmp")
        os.close(fd)
        tmp_path = Path(tmp_name)
        tmp_path.write_text(updated_text, encoding="utf-8")
        tmp_path.replace(prefs_path)
        tmp_path = None
        return True
    except OSError:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def _sqlite_backup_file(src_db: Path, dst_db: Path) -> None:
    """Safely snapshot a live (possibly WAL-mode) SQLite DB into dst_db."""
    src_conn = sqlite3.connect(f"{src_db.as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        dst_conn = sqlite3.connect(str(dst_db), timeout=5.0)
        try:
            src_conn.backup(dst_conn)
            dst_conn.commit()
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _copy_idb_origin_dir(src_dir: Path, dst_parent: Path, folder_name: str) -> None:
    """Copy a moz-extension+++<uuid>* storage folder atomically using SQLite backup for .sqlite files."""
    raw_dst = dst_parent / folder_name
    if _is_link_or_junction(raw_dst):
        raise ValueError("Destination extension storage path is a link.")
    dst_dir = safe_child(dst_parent, folder_name)

    stage_dir = Path(tempfile.mkdtemp(dir=dst_parent, prefix=".sync-"))
    backup_dir: Path | None = None
    try:
        src_root = src_dir.resolve()
        total_files = 0
        total_bytes = 0
        for root, dirs, files in os.walk(src_root, followlinks=False):
            root_path = Path(root)
            if not root_path.resolve().is_relative_to(src_root):
                continue
            rel = root_path.relative_to(src_root)
            safe_dirs: list[str] = []
            for d in dirs:
                dp = root_path / d
                if not _is_link_or_junction(dp) and dp.resolve().is_relative_to(src_root):
                    safe_dirs.append(d)
            dirs[:] = safe_dirs
            target_dir = stage_dir / rel
            target_dir.mkdir(parents=True, exist_ok=True)
            for fname in files:
                lower = fname.lower()
                if lower.endswith((".sqlite-wal", ".sqlite-shm", ".sqlite-journal")):
                    continue
                src_file = root_path / fname
                if _is_link_or_junction(src_file) or not src_file.resolve().is_relative_to(src_root):
                    continue
                fsize = src_file.stat().st_size
                total_files += 1
                total_bytes += fsize
                if total_files > MAX_EXT_STORAGE_FILES or total_bytes > MAX_EXT_STORAGE_BYTES:
                    raise ValueError("Extension storage exceeds copy safety cap.")
                dst_file = target_dir / fname
                if lower.endswith(".sqlite"):
                    _sqlite_backup_file(src_file, dst_file)
                else:
                    shutil.copy2(src_file, dst_file)

        # Atomic swap: rename existing dst_dir to a sibling backup first (fails cleanly if app has it locked).
        if dst_dir.exists():
            backup_dir = dst_parent / f".bak-{stage_dir.name}"
            dst_dir.rename(backup_dir)
        try:
            stage_dir.rename(dst_dir)
            stage_dir = None  # type: ignore[assignment]
        except OSError:
            if backup_dir is not None and backup_dir.exists() and not dst_dir.exists():
                try:
                    backup_dir.rename(dst_dir)
                    backup_dir = None
                except OSError:
                    pass
            raise
        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
            backup_dir = None
    finally:
        if stage_dir is not None and stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)


def _unlink_sqlite_sidecars(db_path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sc = Path(f"{db_path}{suffix}")
        if sc.exists() and not _is_link_or_junction(sc):
            try:
                sc.unlink(missing_ok=True)
            except OSError:
                pass


def sync_storage_sync_db(src_profile: Path, profile_dir: Path, active_ext_ids: set[str]) -> str | None:
    """Copy browser.storage.sync entries for active extensions from main Zen into the app profile."""
    src_db = src_profile / "storage-sync-v2.sqlite"
    if not src_db.is_file() or _is_link_or_junction(src_db) or not active_ext_ids:
        return None
    raw_dst = profile_dir / "storage-sync-v2.sqlite"
    if _is_link_or_junction(raw_dst):
        return "Skipped storage.sync copy because destination is a link."

    tmp_path: Path | None = None
    try:
        dst_db = safe_child(profile_dir, "storage-sync-v2.sqlite")
        fd, tmp_name = tempfile.mkstemp(dir=profile_dir, prefix=".sync-v2-", suffix=".sqlite")
        os.close(fd)
        tmp_path = Path(tmp_name)
        _sqlite_backup_file(src_db, tmp_path)

        placeholders = ", ".join("?" for _ in active_ext_ids)
        params = list(active_ext_ids)
        conn = sqlite3.connect(str(tmp_path), timeout=5.0)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "storage_sync_data" in tables:
                conn.execute(f"DELETE FROM storage_sync_data WHERE ext_id NOT IN ({placeholders})", params)
            if "storage_sync_mirror" in tables:
                conn.execute(
                    f"DELETE FROM storage_sync_mirror WHERE ext_id IS NOT NULL AND ext_id NOT IN ({placeholders})",
                    params,
                )
            if "storage_sync_staging" in tables:
                conn.execute("DELETE FROM storage_sync_staging")
            conn.commit()
            conn.execute("VACUUM")
        finally:
            conn.close()

        tmp_path.replace(dst_db)
        tmp_path = None
        _unlink_sqlite_sidecars(dst_db)
        return None
    except (OSError, ValueError, sqlite3.Error):
        if tmp_path is not None:
            _unlink_sqlite_sidecars(tmp_path)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return "Could not update extension storage.sync (close the app and retry)."


def _sync_idb_storage_dirs(
    src_profile_res: Path,
    profile_dir: Path,
    uuids: list[str],
) -> tuple[int, str | None]:
    src_storage_root = src_profile_res / "storage"
    src_storage_raw = src_storage_root / "default"
    if not src_storage_raw.is_dir() or _is_link_or_junction(src_storage_root) or _is_link_or_junction(src_storage_raw):
        return (0, None)
    src_storage_dir = src_storage_raw.resolve()
    if not src_storage_dir.is_relative_to(src_profile_res):
        return (0, None)

    dst_storage_root = profile_dir / "storage"
    dst_default_raw = dst_storage_root / "default"
    if _is_link_or_junction(dst_storage_root) or _is_link_or_junction(dst_default_raw):
        return (0, "Skipped extension storage because app storage path is a link.")
    try:
        dst_storage_dir = safe_child(safe_child(profile_dir, "storage"), "default")
        dst_storage_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError):
        return (0, "Could not create app extension storage directory.")

    copied = 0
    failed = 0
    for uid in uuids:
        for suffix in ("", "^userContextId=4294967295"):
            folder_name = f"moz-extension+++{uid}{suffix}"
            src_origin = src_storage_dir / folder_name
            if not src_origin.is_dir() or _is_link_or_junction(src_origin):
                continue
            try:
                if not src_origin.resolve().is_relative_to(src_storage_dir):
                    continue
                _copy_idb_origin_dir(src_origin, dst_storage_dir, folder_name)
                copied += 1
            except (OSError, ValueError, sqlite3.Error):
                failed += 1
    warn = f"Could not sync {failed} extension storage folder(s) (close the app and retry)." if failed else None
    return (copied, warn)


def sync_zen_extension_settings(
    src_profile: Path,
    profile_dir: Path,
    active_ext_ids: set[str],
) -> tuple[int, str | None]:
    """Sync extension settings (UUID map, storage.local/IndexedDB, and storage.sync) from main Zen."""
    if not active_ext_ids:
        return (0, None)

    src_profile_res = src_profile.resolve()
    src_uuids = read_webext_uuids(src_profile_res / "prefs.js")
    active_lower = {e.lower() for e in active_ext_ids}
    blocked_lower = {e.lower() for e in SENSITIVE_STORAGE_EXTENSION_IDS} | {SELF_EXTENSION_ID.lower()}
    matched_uuids = {
        ext_id: uid
        for ext_id, uid in src_uuids.items()
        if ext_id.lower() in active_lower and ext_id.lower() not in blocked_lower
    }
    if not matched_uuids:
        return (0, None)

    warnings: list[str] = []
    if not merge_webext_uuids_into_prefs(profile_dir, matched_uuids):
        warnings.append("Could not update extension UUIDs in app prefs.js.")

    copied_dirs, idb_warn = _sync_idb_storage_dirs(src_profile_res, profile_dir, list(matched_uuids.values()))
    if idb_warn:
        warnings.append(idb_warn)

    sync_warn = sync_storage_sync_db(src_profile_res, profile_dir, set(matched_uuids.keys()))
    if sync_warn:
        warnings.append(sync_warn)

    return (copied_dirs, "; ".join(warnings) if warnings else None)


def sync_zen_extensions(
    profile_dir: Path,
    candidates: list[Path] | None = None,
    src_profile: Path | None = None,
) -> tuple[int, str | None]:
    """Copy active profile extensions (.xpi) and their settings from main Zen into the app profile."""
    if src_profile is None:
        src_profile = find_default_zen_profile()
    if candidates is None:
        if not src_profile:
            return (0, "Default Zen profile not found; skipped extension sync.")
        candidates = collect_zen_extension_xpis(src_profile)

    dst_ext_raw = profile_dir / "extensions"
    if _is_link_or_junction(dst_ext_raw):
        return (0, "Skipped extension sync because app extensions path is a link.")
    try:
        dst_ext_dir = safe_child(profile_dir, "extensions")
        dst_ext_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError):
        return (0, "Could not create app extensions directory.")

    active_names = {p.name.lower() for p in candidates}
    for existing in dst_ext_dir.glob("*.xpi"):
        try:
            if not _is_link_or_junction(existing) and existing.name.lower() not in active_names:
                existing.unlink(missing_ok=True)
        except OSError:
            pass

    synced = 0
    failed = 0
    for src_xpi in candidates:
        tmp_path: Path | None = None
        try:
            if _is_link_or_junction(src_xpi):
                continue
            dst_xpi = safe_child(dst_ext_dir, src_xpi.name)
            if _is_link_or_junction(dst_xpi):
                failed += 1
                continue
            src_stat = src_xpi.stat()
            if dst_xpi.is_file():
                dst_stat = dst_xpi.stat()
                if dst_stat.st_size == src_stat.st_size and int(dst_stat.st_mtime) == int(src_stat.st_mtime):
                    synced += 1
                    continue
            fd, tmp_name = tempfile.mkstemp(dir=dst_ext_dir, prefix=".tmp-", suffix=".xpi")
            os.close(fd)
            tmp_path = Path(tmp_name)
            shutil.copy2(src_xpi, tmp_path)
            tmp_path.replace(dst_xpi)
            tmp_path = None
            synced += 1
        except (OSError, ValueError):
            failed += 1
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    warnings: list[str] = []
    if failed:
        warnings.append(f"Could not copy {failed} extension(s) into app profile.")
    if src_profile is not None and candidates:
        active_ids = {p.stem for p in candidates}
        _, settings_warn = sync_zen_extension_settings(src_profile, profile_dir, active_ids)
        if settings_warn:
            warnings.append(settings_warn)

    return (synced, "; ".join(warnings) if warnings else None)


def ensure_dirs() -> None:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCHERS_DIR.mkdir(parents=True, exist_ok=True)
    START_MENU_DIR.mkdir(parents=True, exist_ok=True)
    DESKTOP_DIR.mkdir(parents=True, exist_ok=True)


def validate_http_url(raw: str, label: str) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"Invalid {label}: must be a string.")
    url = raw.strip()
    if not url or len(url) > 2048:
        raise ValueError(f"Invalid {label}: empty or too long.")
    if any(ch in url for ch in ('"', "\\", "\r", "\n", "\x00", "\t")):
        raise ValueError(f"Invalid {label}: contains forbidden characters.")
    if url.startswith("-"):
        raise ValueError(f"Invalid {label}: cannot start with '-'.")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Invalid {label}: only http/https URLs are allowed.")
    return url


def assert_public_url(url: str) -> None:
    """Block SSRF to loopback, private LAN, link-local, and metadata IPs."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only public http/https URLs are allowed.")
    infos = socket.getaddrinfo(
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    )
    if not infos:
        raise ValueError("Unable to resolve icon host.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(f"Blocked non-public icon address: {ip}")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self._redirects = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._redirects += 1
        if self._redirects > MAX_REDIRECTS:
            raise ValueError("Too many redirects while fetching icon.")
        target = urllib.parse.urljoin(req.full_url, newurl)
        validate_http_url(target, "icon redirect")
        assert_public_url(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


def sanitize_app_name(raw: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(raw or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")[:60]
    if not cleaned or cleaned.split(".")[0].upper() in WINDOWS_RESERVED_NAMES:
        cleaned = re.sub(r"[^A-Za-z0-9 ._-]", "", fallback).strip(" .")[:60] or "Zen App"
    if cleaned.split(".")[0].upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"{cleaned} App"
    return cleaned


def make_app_id(name: str, origin: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "app"
    digest = hashlib.sha256(origin.lower().encode("utf-8")).hexdigest()[:8]
    app_id = f"{slug}-{digest}"
    if not APP_ID_RE.match(app_id):
        app_id = f"app-{digest}"
    return app_id


def validate_app_id(app_id: Any) -> str:
    if not isinstance(app_id, str) or not APP_ID_RE.match(app_id):
        raise ValueError("Invalid app id.")
    return app_id


def safe_child(parent: Path, child_name: str) -> Path:
    parent_res = parent.resolve()
    target = (parent_res / child_name).resolve()
    if not target.is_relative_to(parent_res) or target == parent_res:
        raise ValueError("Resolved path escaped target directory.")
    return target


def choose_shortcut_path(
    parent_dir: Path,
    base_name: str,
    app_id: str,
    existing_apps: list[dict[str, Any]],
    shortcut_key: str,
) -> Path:
    """Avoid overwriting shortcuts owned by another app or untracked user shortcuts."""
    own_existing = next((a.get(shortcut_key) for a in existing_apps if a.get("id") == app_id), None)
    owned_by_others = {
        str(Path(a[shortcut_key]).resolve()).lower()
        for a in existing_apps
        if a.get("id") != app_id and a.get(shortcut_key)
    }

    candidate = safe_child(parent_dir, f"{base_name}.lnk")
    cand_key = str(candidate).lower()
    if own_existing and str(Path(own_existing).resolve()).lower() == cand_key:
        return candidate
    if cand_key not in owned_by_others and not candidate.exists():
        return candidate

    suffix = app_id.rsplit("-", 1)[-1][:8]
    return safe_child(parent_dir, f"{base_name} ({suffix}).lnk")


def load_apps() -> list[dict[str, Any]]:
    if not APPS_DB_PATH.is_file():
        return []
    raw = APPS_DB_PATH.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("apps.json is corrupted (expected a JSON list).")
    return data


def save_apps(apps: list[dict[str, Any]]) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = APPS_DB_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(apps, indent=2), encoding="utf-8")
    tmp.replace(APPS_DB_PATH)


def parse_hex_color(raw: str) -> tuple[int, int, int]:
    m = re.match(r"^#?([0-9a-fA-F]{6})$", str(raw or "").strip())
    if not m:
        return (37, 99, 235)
    h = m.group(1)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def create_fallback_icon(name: str, theme_color: str, dest_ico: Path) -> None:
    rgb = parse_hex_color(theme_color)
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([(8, 8), (248, 248)], radius=52, fill=(*rgb, 255))
    letter = (name.strip()[:1] or "Z").upper()
    try:
        font = ImageFont.truetype("segoeui.ttf", 140)
    except Exception:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), letter, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    draw.text(((256 - w) / 2 - bbox[0], (256 - h) / 2 - bbox[1]), letter, fill=(255, 255, 255, 255), font=font)
    img.save(dest_ico, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def download_and_build_icon(icon_url: str | None, name: str, theme_color: str, dest_ico: Path) -> None:
    if icon_url:
        try:
            valid_icon = validate_http_url(icon_url, "iconUrl")
            assert_public_url(valid_icon)
            opener = urllib.request.build_opener(SafeRedirectHandler())
            req = urllib.request.Request(
                valid_icon,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ZenPWA/1.1"},
            )
            with opener.open(req, timeout=10) as resp:
                raw = resp.read(MAX_ICON_BYTES + 1)
            if len(raw) <= MAX_ICON_BYTES:
                img = Image.open(io.BytesIO(raw))
                if img.width <= MAX_ICON_DIMENSION and img.height <= MAX_ICON_DIMENSION:
                    img = img.convert("RGBA")
                    img.thumbnail((256, 256), Image.Resampling.LANCZOS)
                    square = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
                    square.paste(img, ((256 - img.width) // 2, (256 - img.height) // 2))
                    square.save(
                        dest_ico,
                        format="ICO",
                        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
                    )
                    return
        except Exception:
            pass
    create_fallback_icon(name, theme_color, dest_ico)


def setup_zen_profile(profile_dir: Path) -> None:
    """Standard Zen PWA app profile template applied to every installed app."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    chrome_dir = profile_dir / "chrome"
    chrome_dir.mkdir(parents=True, exist_ok=True)

    user_js = """// Generated by Zen PWA for standalone app mode
// Merge window controls into a single compact top app bar (Chromium PWA style)
user_pref("browser.tabs.inTitlebar", 1);
// Give each Zen PWA profile its own Windows Taskbar button separate from main Zen Browser
user_pref("taskbar.grouping.useprofile", true);
user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);
user_pref("zen.welcome-screen.seen", true);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.firstRunURL", "");
user_pref("browser.tabs.warnOnClose", false);
user_pref("zen.view.use-single-toolbar", false);
// Use Zen's native compact Extensions grid inside #zen-site-data-icon-button so extension popups anchor properly
user_pref("zen.theme.hide-unified-extensions-button", true);
user_pref("zen.view.compact.enable-at-startup", false);
user_pref("zen.view.compact.should-enable-at-startup", false);
user_pref("zen.view.compact.hide-tabbar", true);
user_pref("zen.view.compact.hide-toolbar", false);
// Auto-enable extensions synced from main Zen profile (profile scope = 1 only)
user_pref("extensions.autoDisableScopes", 14);
user_pref("extensions.startupScanScopes", 1);
"""
    (profile_dir / "user.js").write_text(user_js, encoding="utf-8")

    user_chrome = """/* Zen PWA standalone app window: single compact 32px titlebar with Back/Forward/Reload, Extensions, and Min/Max/Close */
#navigator-toolbox,
#zen-sidebar-splitter,
#PersonalToolbar,
#zen-sidebar-top-buttons,
#zen-sidebar-foot-buttons {
  display: none !important;
  visibility: collapse !important;
  width: 0 !important;
  min-width: 0 !important;
  height: 0 !important;
  min-height: 0 !important;
}

#zen-appcontent-navbar-wrapper {
  display: flex !important;
  visibility: visible !important;
  min-height: 32px !important;
  max-height: 32px !important;
  padding: 0 !important;
  margin: 0 !important;
  border-bottom: 1px solid color-mix(in srgb, currentColor 10%, transparent) !important;
  -moz-window-dragging: drag !important;
}

/* Hide the compact app bar completely in video/DOM fullscreen and F11 fullscreen */
:root[inDOMFullscreen="true"] #zen-appcontent-navbar-wrapper,
:root[inFullscreen="true"] #zen-appcontent-navbar-wrapper {
  display: none !important;
}

#nav-bar {
  display: flex !important;
  align-items: center !important;
  flex: 1 1 auto !important;
  min-width: 0 !important;
  min-height: 32px !important;
  max-height: 32px !important;
  padding: 0 0 0 6px !important;
  margin: 0 !important;
  background: transparent !important;
  box-shadow: none !important;
  -moz-window-dragging: drag !important;
}

#nav-bar > :not(#nav-bar-customization-target):not(.titlebar-buttonbox-container) {
  display: none !important;
}

#nav-bar-customization-target {
  display: flex !important;
  align-items: center !important;
  flex: 1 1 auto !important;
  min-width: 0 !important;
  height: 32px !important;
  gap: 2px !important;
  -moz-window-dragging: drag !important;
}

#nav-bar-customization-target > :not(#back-button):not(#forward-button):not(#stop-reload-button):not(#reload-button):not(#urlbar-container) {
  display: none !important;
}

#back-button,
#forward-button,
#stop-reload-button,
#reload-button {
  -moz-window-dragging: no-drag !important;
  pointer-events: auto !important;
  --toolbarbutton-inner-padding: 5px !important;
  border-radius: 6px !important;
}

/* Collapse the URL bar down to only Zen's native #zen-site-data-icon-button (compact Extensions grid + popup anchor) */
#urlbar-container {
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  flex: 0 0 28px !important;
  width: 28px !important;
  min-width: 28px !important;
  max-width: 28px !important;
  min-height: 0 !important;
  height: 32px !important;
  padding: 0 !important;
  margin: 0 6px 0 auto !important;
  -moz-window-dragging: no-drag !important;
}

#urlbar,
#urlbar[breakout],
#urlbar[breakout-extend],
#urlbar[open] {
  position: static !important;
  inset: auto !important;
  transform: none !important;
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  width: 28px !important;
  min-width: 28px !important;
  max-width: 28px !important;
  min-height: 26px !important;
  height: 26px !important;
  margin: 0 !important;
  padding: 0 !important;
  overflow: hidden !important;
  background: transparent !important;
  border: none !important;
  box-shadow: none !important;
  --urlbar-box-bgcolor: transparent !important;
  --urlbar-box-focus-bgcolor: transparent !important;
  -moz-window-dragging: no-drag !important;
}

#urlbar .urlbar-background,
#urlbar .urlbar-input-box,
#urlbar .urlbarView,
#urlbar .urlbar-input-container > :not(#identity-box),
#urlbar #urlbar-input,
#urlbar #urlbar-scheme,
#urlbar #urlbar-search-mode-indicator,
#urlbar #urlbar-label-box,
#urlbar #urlbar-revert-button-container,
#urlbar #page-action-buttons,
#urlbar #tracking-protection-icon-container,
#urlbar #trust-icon-container,
#urlbar #remote-control-box,
#urlbar #notification-popup-box,
#urlbar #zen-copy-url-button,
#urlbar .search-one-offs {
  display: none !important;
}

#identity-box {
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  width: 26px !important;
  height: 26px !important;
  margin: 0 !important;
  padding: 0 !important;
}

#identity-box > :not(#zen-site-data-icon-button) {
  display: none !important;
}

#zen-site-data-icon-button {
  display: flex !important;
  visibility: visible !important;
  align-items: center !important;
  justify-content: center !important;
  width: 26px !important;
  height: 26px !important;
  margin: 0 !important;
  padding: 0 !important;
  border-radius: 6px !important;
  -moz-window-dragging: no-drag !important;
  pointer-events: auto !important;
  cursor: pointer !important;
}

#zen-site-data-icon-button:hover {
  background-color: color-mix(in srgb, currentColor 12%, transparent) !important;
}

#zen-site-data-icon-button image {
  width: 16px !important;
  height: 16px !important;
  fill: currentColor !important;
}

.titlebar-buttonbox-container {
  display: flex !important;
  visibility: visible !important;
  align-items: stretch !important;
  flex-shrink: 0 !important;
  height: 32px !important;
  margin: 0 !important;
  -moz-window-dragging: no-drag !important;
  pointer-events: auto !important;
}

.titlebar-buttonbox {
  height: 32px !important;
}

#zen-main-app-wrapper,
#zen-tabbox-wrapper,
#tabbrowser-tabbox {
  margin: 0 !important;
  padding: 0 !important;
  border-radius: 0 !important;
}
"""
    (chrome_dir / "userChrome.css").write_text(user_chrome, encoding="utf-8")


def compile_cs_launcher(cs_code: str, out_path: Path, icon_path: Path | None) -> Path | None:
    if not CSC_EXE.is_file():
        return None
    try:
        with tempfile.TemporaryDirectory(dir=LAUNCHERS_DIR) as tmp_dir:
            tmp_root = Path(tmp_dir)
            cs_path = tmp_root / "launcher.cs"
            out_exe = tmp_root / "launcher.exe"
            cs_path.write_text(cs_code, encoding="utf-8")
            cmd = [
                str(CSC_EXE),
                "/nologo",
                "/noconfig",
                "/optimize+",
                "/target:winexe",
                "/r:System.dll",
                "/r:System.Management.dll",
                f"/out:{out_exe}",
            ]
            if icon_path is not None and icon_path.is_file():
                cmd.append(f"/win32icon:{icon_path}")
            cmd.append(str(cs_path))
            subprocess.run(
                cmd,
                cwd=str(LAUNCHERS_DIR),
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=20,
            )
            out_exe.replace(out_path)
            return out_path
    except (OSError, subprocess.SubprocessError):
        return None


def make_smart_launcher_cs(
    zen_exe: Path,
    launch_args: str,
    match_substring: str,
    invert_match: bool,
) -> str:
    b64_zen = base64.b64encode(str(zen_exe).encode("utf-8")).decode("ascii")
    b64_args = base64.b64encode(launch_args.encode("utf-8")).decode("ascii")
    b64_match = base64.b64encode(match_substring.lower().encode("utf-8")).decode("ascii")
    inv_lit = "true" if invert_match else "false"

    return f"""using System;
using System.Diagnostics;
using System.Management;
using System.Runtime.InteropServices;
using System.Text;

class ZenPwaLauncher {{
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hWnd);
    const int SW_RESTORE = 9;

    static string Dec(string b64) {{
        return Encoding.UTF8.GetString(Convert.FromBase64String(b64));
    }}

    static bool FocusExisting(string needle, bool invert) {{
        try {{
            using (var searcher = new ManagementObjectSearcher(
                "SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name = 'zen.exe'")) {{
                foreach (ManagementObject obj in searcher.Get()) {{
                    string cmd = (obj["CommandLine"] as string ?? "").ToLowerInvariant();
                    if (cmd.Contains("-contentproc")) continue;
                    bool matches = cmd.Contains(needle);
                    if (invert ? !matches : matches) {{
                        int pid = Convert.ToInt32(obj["ProcessId"]);
                        try {{
                            var proc = Process.GetProcessById(pid);
                            if (proc.MainWindowHandle != IntPtr.Zero) {{
                                if (IsIconic(proc.MainWindowHandle)) ShowWindow(proc.MainWindowHandle, SW_RESTORE);
                                SetForegroundWindow(proc.MainWindowHandle);
                                return true;
                            }}
                        }} catch {{}}
                    }}
                }}
            }}
        }} catch {{}}
        return false;
    }}

    static void Main() {{
        string zen = Dec("{b64_zen}");
        string args = Dec("{b64_args}");
        string needle = Dec("{b64_match}");
        if (FocusExisting(needle, {inv_lit})) return;
        var psi = new ProcessStartInfo();
        psi.FileName = zen;
        psi.Arguments = args;
        psi.UseShellExecute = false;
        Process.Start(psi);
    }}
}}
"""


def build_launcher_exe(
    launcher_exe: Path,
    zen_exe: Path,
    profile_dir: Path,
    start_url: str,
    icon_path: Path,
) -> Path | None:
    """Compile a tiny per-app GUI .exe so Raycast treats each Zen app separately from zen.exe."""
    args = f'-new-instance -no-remote -profile "{profile_dir}" --new-window "{start_url}"'
    cs_code = make_smart_launcher_cs(zen_exe, args, str(profile_dir), invert_match=False)
    return compile_cs_launcher(cs_code, launcher_exe, icon_path)


def ensure_main_zen_launcher(zen_exe: Path) -> None:
    """Create a dedicated Zen Browser (Main) launcher so Raycast can open Zen even when a Zen App is already open."""
    main_exe = safe_child(LAUNCHERS_DIR, "zen-browser-main.exe")
    cs_code = make_smart_launcher_cs(zen_exe, "", str(PROFILES_DIR), invert_match=True)
    compiled = compile_cs_launcher(cs_code, main_exe, None)
    if compiled is None:
        return
    lnk_path = safe_child(START_MENU_PROGRAMS_DIR, "Zen Browser.lnk")
    create_windows_shortcut(lnk_path, compiled, "", zen_exe, "Zen Browser (Main — Raycast safe)")


def create_windows_shortcut(
    lnk_path: Path,
    target_exe: Path,
    arguments: str,
    icon_path: Path,
    description: str,
) -> None:
    env = os.environ.copy()
    env["ZENPWA_LNK_PATH"] = str(lnk_path)
    env["ZENPWA_TARGET"] = str(target_exe)
    env["ZENPWA_ARGS"] = arguments
    env["ZENPWA_ICON"] = f"{icon_path},0"
    env["ZENPWA_WORKDIR"] = str(target_exe.parent)
    env["ZENPWA_DESC"] = description

    ps_script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$sc = $ws.CreateShortcut($env:ZENPWA_LNK_PATH); "
        "$sc.TargetPath = $env:ZENPWA_TARGET; "
        "$sc.Arguments = $env:ZENPWA_ARGS; "
        "$sc.IconLocation = $env:ZENPWA_ICON; "
        "$sc.WorkingDirectory = $env:ZENPWA_WORKDIR; "
        "$sc.Description = $env:ZENPWA_DESC; "
        "$sc.Save()"
    )
    subprocess.run(
        [str(POWERSHELL_EXE), "-NoProfile", "-NonInteractive", "-Command", ps_script],
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=15,
    )


def launch_zen_app(zen_exe: Path, profile_dir: Path, start_url: str) -> None:
    cmd = [
        str(zen_exe),
        "-new-instance",
        "-no-remote",
        "-profile",
        str(profile_dir),
        "--new-window",
        start_url,
    ]
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
        subprocess, "DETACHED_PROCESS", 0
    )
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )


def remove_shortcut_if_inside(raw_path: str | None, parent_dir: Path) -> None:
    if not raw_path:
        return
    p = Path(raw_path).resolve()
    if p.is_relative_to(parent_dir.resolve()) and p.suffix.lower() == ".lnk" and p.exists():
        p.unlink()


def handle_install(msg: dict[str, Any]) -> dict[str, Any]:
    ensure_dirs()
    zen_exe = find_zen_exe()

    start_url = validate_http_url(msg.get("startUrl", ""), "startUrl")
    parsed_start = urllib.parse.urlparse(start_url)
    origin_raw = msg.get("origin") or start_url
    parsed_origin = urllib.parse.urlparse(validate_http_url(origin_raw, "origin"))
    if (parsed_start.scheme, parsed_start.netloc.lower()) != (
        parsed_origin.scheme,
        parsed_origin.netloc.lower(),
    ):
        raise ValueError("startUrl must have the same origin as the installed site.")
    origin = f"{parsed_origin.scheme}://{parsed_origin.netloc}"

    existing_apps = load_apps()
    name = sanitize_app_name(msg.get("name", ""), parsed_origin.hostname or "Zen App")
    app_id = make_app_id(name, origin)
    prev_record = next((a for a in existing_apps if a.get("id") == app_id), None)

    profile_dir = safe_child(PROFILES_DIR, app_id)
    icon_path = safe_child(ICONS_DIR, f"{app_id}.ico")
    start_menu_lnk = choose_shortcut_path(START_MENU_DIR, name, app_id, existing_apps, "startMenuShortcut")
    want_desktop = bool(msg.get("createDesktopShortcut"))
    desktop_lnk = (
        choose_shortcut_path(DESKTOP_DIR, name, app_id, existing_apps, "desktopShortcut")
        if want_desktop
        else None
    )

    setup_zen_profile(profile_dir)
    extensions_synced, extension_warning = sync_zen_extensions(profile_dir)

    cookies_synced = 0
    cookie_warning = None
    if msg.get("syncSession", True):
        cookies_synced, cookie_warning = sync_domain_cookies(profile_dir, start_url)

    download_and_build_icon(msg.get("iconUrl"), name, msg.get("themeColor", ""), icon_path)
    ensure_main_zen_launcher(zen_exe)

    launcher_exe = safe_child(LAUNCHERS_DIR, f"{app_id}.exe")
    compiled_launcher = build_launcher_exe(launcher_exe, zen_exe, profile_dir, start_url, icon_path)

    if compiled_launcher is not None:
        shortcut_target = compiled_launcher
        shortcut_args = ""
    else:
        try:
            launcher_exe.unlink(missing_ok=True)
        except OSError:
            pass
        shortcut_target = zen_exe
        shortcut_args = f'-new-instance -no-remote -profile "{profile_dir}" --new-window "{start_url}"'

    create_windows_shortcut(start_menu_lnk, shortcut_target, shortcut_args, icon_path, f"{name} (Zen PWA)")
    if desktop_lnk is not None:
        create_windows_shortcut(desktop_lnk, shortcut_target, shortcut_args, icon_path, f"{name} (Zen PWA)")
    elif prev_record and prev_record.get("desktopShortcut"):
        remove_shortcut_if_inside(prev_record.get("desktopShortcut"), DESKTOP_DIR)

    record = {
        "id": app_id,
        "name": name,
        "startUrl": start_url,
        "origin": origin,
        "profileDir": str(profile_dir),
        "iconPath": str(icon_path),
        "launcherExe": str(compiled_launcher) if compiled_launcher else None,
        "startMenuShortcut": str(start_menu_lnk),
        "desktopShortcut": str(desktop_lnk) if desktop_lnk else None,
    }

    apps = [a for a in existing_apps if a.get("id") != app_id]
    apps.append(record)
    save_apps(apps)

    if msg.get("launchAfterInstall"):
        launch_zen_app(zen_exe, profile_dir, start_url)

    return {
        "ok": True,
        "app": record,
        "cookiesSynced": cookies_synced,
        "cookieWarning": cookie_warning,
        "extensionsSynced": extensions_synced,
        "extensionWarning": extension_warning,
    }


def handle_launch(msg: dict[str, Any]) -> dict[str, Any]:
    app_id = validate_app_id(msg.get("id"))
    apps = load_apps()
    app = next((a for a in apps if a.get("id") == app_id), None)
    if not app:
        return {"ok": False, "error": f"App '{app_id}' is not installed."}

    zen_exe = find_zen_exe()
    profile_dir = safe_child(PROFILES_DIR, app_id)
    start_url = validate_http_url(app.get("startUrl", ""), "startUrl")
    # Refresh standard window template and extensions on launch so existing apps stay up to date.
    setup_zen_profile(profile_dir)
    extensions_synced, extension_warning = sync_zen_extensions(profile_dir)
    launch_zen_app(zen_exe, profile_dir, start_url)
    return {
        "ok": True,
        "id": app_id,
        "extensionsSynced": extensions_synced,
        "extensionWarning": extension_warning,
    }


def handle_sync_all() -> dict[str, Any]:
    """Refresh profile template and synced extensions across all installed apps."""
    apps = load_apps()
    src_profile = find_default_zen_profile()
    candidates = collect_zen_extension_xpis(src_profile) if src_profile else None
    results = []
    for app in apps:
        raw_id = app.get("id") if isinstance(app, dict) else None
        try:
            app_id = validate_app_id(raw_id)
            profile_dir = safe_child(PROFILES_DIR, app_id)
            setup_zen_profile(profile_dir)
            ext_count, ext_warn = sync_zen_extensions(profile_dir, candidates=candidates, src_profile=src_profile)
            results.append({"id": app_id, "extensionsSynced": ext_count, "warning": ext_warn})
        except (OSError, ValueError, AttributeError) as exc:
            results.append({"id": raw_id, "extensionsSynced": 0, "warning": str(exc)})
    return {"ok": True, "syncedApps": results}


def handle_uninstall(msg: dict[str, Any]) -> dict[str, Any]:
    app_id = validate_app_id(msg.get("id"))
    apps = load_apps()
    app = next((a for a in apps if a.get("id") == app_id), None)
    if not app:
        return {"ok": False, "error": f"App '{app_id}' not found."}

    prof_p = safe_child(PROFILES_DIR, app_id)
    if prof_p.exists():
        try:
            shutil.rmtree(prof_p)
        except OSError as exc:
            return {
                "ok": False,
                "error": f"Close the running '{app['name']}' window first before removing it ({exc}).",
            }

    remove_shortcut_if_inside(app.get("startMenuShortcut"), START_MENU_DIR)
    remove_shortcut_if_inside(app.get("desktopShortcut"), DESKTOP_DIR)

    for artifact in (safe_child(ICONS_DIR, f"{app_id}.ico"), safe_child(LAUNCHERS_DIR, f"{app_id}.exe")):
        try:
            artifact.unlink(missing_ok=True)
        except OSError:
            pass

    save_apps([a for a in apps if a.get("id") != app_id])
    return {"ok": True, "id": app_id}


def dispatch(msg: dict[str, Any]) -> dict[str, Any]:
    action = msg.get("action")
    if action == "ping":
        zen_exe = find_zen_exe()
        return {"ok": True, "version": VERSION, "zenPath": str(zen_exe)}
    if action == "list":
        return {"ok": True, "apps": load_apps()}
    if action == "install":
        return handle_install(msg)
    if action == "launch":
        return handle_launch(msg)
    if action == "uninstall":
        return handle_uninstall(msg)
    if action == "sync_all":
        return handle_sync_all()
    return {"ok": False, "error": f"Unknown action: {action}"}


def read_native_message() -> dict[str, Any] | None:
    raw_len = sys.stdin.buffer.read(4)
    if not raw_len or len(raw_len) < 4:
        return None
    msg_len = struct.unpack("<I", raw_len)[0]
    if msg_len == 0 or msg_len > MAX_MESSAGE_BYTES:
        raise ValueError(f"Invalid native message length: {msg_len}")
    raw_body = sys.stdin.buffer.read(msg_len)
    if len(raw_body) < msg_len:
        return None
    return json.loads(raw_body.decode("utf-8"))


def write_native_message(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--cli":
        try:
            req = json.loads(sys.argv[2])
            res = dispatch(req)
        except Exception as exc:
            res = {"ok": False, "error": str(exc)}
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1

    try:
        while True:
            msg = read_native_message()
            if msg is None:
                break
            try:
                resp = dispatch(msg)
            except Exception as exc:
                resp = {"ok": False, "error": str(exc)}
            write_native_message(resp)
    except Exception as exc:
        try:
            write_native_message({"ok": False, "error": str(exc)})
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
