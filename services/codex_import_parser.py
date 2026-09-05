"""Pure parser for Codex2API account import formats.

Contract pinned to Codex2API source revision f6cbdee309c9606b8887adb7fb9840a434ce77ef
(and its staging v2.6 import tests).  Parsing is deliberately side effect free;
database duplicate handling remains the caller's responsibility.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

MAX_IMPORT_BYTES = 20 * 1024 * 1024


class ImportFormatError(ValueError):
    """Input is malformed for the selected import format."""


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _scalar(value: Any) -> str:
    """Match Go importJSONScalarString (string/number/bool only)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return _text(value)
    return ""


def _first(*values: Any) -> str:
    for value in values:
        item = _text(value)
        if item:
            return item
    return ""


def _user(obj: Mapping[str, Any]) -> Mapping[str, Any]:
    value = obj.get("user")
    return value if isinstance(value, Mapping) else {}


def _account(obj: Mapping[str, Any]) -> Mapping[str, Any]:
    value = obj.get("account")
    return value if isinstance(value, Mapping) else {}


def _row_from_entry(entry: Mapping[str, Any], *, fallback_name: str = "") -> dict[str, Any] | None:
    user = _user(entry)
    account = _account(entry)
    rt = _text(entry.get("refresh_token"))
    st = _first(entry.get("session_token"), entry.get("sessionToken"))
    at = _first(entry.get("access_token"), entry.get("accessToken"))
    if not (rt or st or at):
        # Agent Identity has no RT/ST/AT but is a valid import credential.
        nested = entry.get("agent_identity") or entry.get("agentIdentity")
        node = nested if isinstance(nested, Mapping) else entry
        runtime = _text(node.get("agent_runtime_id"))
        private = _text(node.get("agent_private_key"))
        if runtime and private:
            row: dict[str, Any] = {"agent_runtime_id": runtime, "agent_private_key": private}
            for out, keys in {
                "agent_task_id": ("task_id",), "chatgpt_user_id": ("chatgpt_user_id",),
                "email": ("email",), "account_id": ("account_id",), "plan_type": ("plan_type",),
            }.items():
                val = _first(*(node.get(k) for k in keys))
                if val: row[out] = val
            name = _first(fallback_name, node.get("email"))
            if name: row["name"] = name
            return row
        return None

    email = _first(entry.get("email"), user.get("email"))
    name = _first(fallback_name, entry.get("name"), user.get("name"), email)
    plan = _first(entry.get("plan_type"), entry.get("planType"), account.get("plan_type"), account.get("planType"))
    identity_id = _first(entry.get("account_id"), user.get("id"), account.get("id"))
    account_id = _text(entry.get("account_id"))
    # Source stores only explicit account_id, while chatgpt_account_id falls back to identity ID.
    chatgpt_id = _first(entry.get("chatgpt_account_id"), identity_id)
    expires = _first(_scalar(entry.get("expires_at")), _scalar(entry.get("expired")), _scalar(entry.get("expires")))
    row: dict[str, Any] = {}
    for key, value in (("refresh_token", rt), ("session_token", st), ("access_token", at),
                       ("name", name), ("email", email), ("id_token", _first(entry.get("id_token"), entry.get("idToken"))),
                       ("account_id", _text(entry.get("account_id"))), ("chatgpt_account_id", chatgpt_id),
                       ("plan_type", plan), ("expires_at", expires),
                       ("codex_7d_used_percent", _scalar(entry.get("codex_7d_used_percent"))),
                       ("codex_7d_reset_at", _text(entry.get("codex_7d_reset_at"))),
                       ("codex_5h_used_percent", _scalar(entry.get("codex_5h_used_percent"))),
                       ("codex_5h_reset_at", _text(entry.get("codex_5h_reset_at"))),
                       ("codex_5h_usage_updated_at", _text(entry.get("codex_5h_usage_updated_at"))),
                       ("codex_usage_updated_at", _text(entry.get("codex_usage_updated_at")))):
        if value:
            row[key] = value
    return row


def _parse_json(data: str) -> list[dict[str, Any]]:
    raw = data.lstrip("\ufeff")
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ImportFormatError("invalid import json") from exc
    entries: list[Mapping[str, Any]] = []
    if isinstance(parsed, list):
        entries = [x for x in parsed if isinstance(x, Mapping)]
    elif isinstance(parsed, Mapping):
        # Sub2API export is {accounts:[{name,credentials:{...}}]}.
        accounts = parsed.get("accounts")
        if isinstance(accounts, list) and any(isinstance(x, Mapping) and isinstance(x.get("credentials"), Mapping) for x in accounts):
            for item in accounts:
                if not isinstance(item, Mapping) or not isinstance(item.get("credentials"), Mapping):
                    continue
                entries.append(dict(item["credentials"], name=item.get("name", "")))
        else:
            entries = [parsed]
    else:
        return []
    rows: list[dict[str, Any]] = []
    normalized_entries: list[Mapping[str, Any]] = []
    for entry in entries:
        # Codex2API exports may wrap one credential in a top-level
        # `credentials` object. Preserve the outer display name while
        # unwrapping the credential fields for the common importer.
        wrapped = entry.get("credentials")
        if isinstance(wrapped, Mapping):
            merged = dict(wrapped)
            for key in ("name", "email"):
                if key not in merged and entry.get(key) not in (None, ""):
                    merged[key] = entry.get(key)
            normalized_entries.append(merged)
        else:
            normalized_entries.append(entry)
    for entry in normalized_entries:
        row = _row_from_entry(entry, fallback_name=_text(entry.get("name")))
        if row is not None:
            rows.append(row)
    return rows


def parse_import_content(content: str, format: str = "txt") -> list[dict[str, Any]]:
    """Parse one uploaded content blob into canonical credential dictionaries.

    Supported format values are ``txt`` (one RT per line), ``json`` (flat
    CLIProxyAPI, Sub2API ``accounts`` export, or ChatGPT session JSON),
    ``json_at`` (JSON with AT only; RT/ST are discarded), and ``at_txt``.
    """
    if not isinstance(content, str):
        raise TypeError("content must be text")
    if len(content.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ImportFormatError("import content exceeds 20MB")
    kind = _text(format).lower() or "txt"
    if kind in {"txt", "at_txt"}:
        key = "access_token" if kind == "at_txt" else "refresh_token"
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for line in content.lstrip("\ufeff").splitlines():
            token = line.strip()
            if token and token not in seen:
                seen.add(token)
                rows.append({key: token})
        return rows
    if kind not in {"json", "json_at"}:
        raise ImportFormatError(f"unsupported import format: {format}")
    rows = _parse_json(content)
    if kind == "json_at":
        return [{k: v for k, v in row.items() if k not in {"refresh_token", "session_token"}} for row in rows if row.get("access_token")]
    return rows


def parse_import_files(files: Mapping[str, str]) -> list[dict[str, Any]]:
    """Parse a folder-style upload, matching the UI's JSON-then-TXT order."""
    if not isinstance(files, Mapping):
        raise TypeError("files must map file names to text")
    rows: list[dict[str, Any]] = []
    seen_txt: set[str] = set()
    for ext in ("json", "txt"):
        for name, content in files.items():
            if _text(name).lower().rsplit(".", 1)[-1:] != [ext]:
                continue
            if not content:
                continue
            parsed = parse_import_content(content, ext)
            if ext == "txt":
                # Go's importTokensFromTextFiles deduplicates token text across
                # every uploaded file while retaining first-seen order.
                parsed = [row for row in parsed if row.get("refresh_token") not in seen_txt]
                seen_txt.update(row["refresh_token"] for row in parsed if row.get("refresh_token"))
                rows.extend(parsed)
            else:
                rows.extend(parsed)
    return rows


__all__ = ["ImportFormatError", "MAX_IMPORT_BYTES", "parse_import_content", "parse_import_files"]
