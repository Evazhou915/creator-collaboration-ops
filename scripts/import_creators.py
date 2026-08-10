#!/usr/bin/env python3
"""Normalize and import a creator list into the active project's Creator Master.

Input may be CSV or TSV. The command is preview-only unless --apply is supplied.
It never sends email or changes a prior project's Bitable.

Required environment variables for --apply:
  FEISHU_APP_ID
  FEISHU_APP_SECRET

The manifest is produced by bootstrap_feishu_bitable.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import requests

API_ROOT = "https://open.feishu.cn/open-apis"
BATCH_SIZE = 500

ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("达人昵称", "昵称", "达人名", "creator", "creator_name", "name", "username"),
    "platform": ("平台", "platform", "渠道"),
    "profile_url": ("主页链接", "主页", "profile_url", "profile", "url", "链接"),
    "followers": ("粉丝数量", "粉丝数", "followers", "follower_count", "粉丝"),
    "email": ("联系邮箱", "邮箱", "email", "mail"),
    "source": ("来源", "source", "source_batch"),
    "tags": ("达人标签", "标签", "tags", "tag", "内容方向"),
    "cpm": ("CPM", "cpm"),
    "quote": ("首次报价", "报价", "quote", "rate", "price"),
    "currency": ("币种", "currency", "货币"),
}

PLATFORM_ALIASES = {
    "tk": "TikTok",
    "tiktok": "TikTok",
    "抖音国际版": "TikTok",
    "ig": "Instagram",
    "instagram": "Instagram",
    "ins": "Instagram",
    "youtube": "YouTube",
    "yt": "YouTube",
    "x": "X",
    "twitter": "X",
}

VALID_PLATFORMS = {"TikTok", "Instagram", "YouTube", "X", "其他"}


def clean(value: Any) -> str:
    return str(value or "").strip()


def header_map(row: dict[str, str]) -> dict[str, str]:
    normalized = {clean(key).casefold(): clean(value) for key, value in row.items()}
    result: dict[str, str] = {}
    for target, aliases in ALIASES.items():
        for alias in aliases:
            value = normalized.get(alias.casefold(), "")
            if value:
                result[target] = value
                break
    return result


def normalize_platform(value: str) -> str:
    key = re.sub(r"\s+", "", clean(value).casefold())
    return PLATFORM_ALIASES.get(key, "其他" if not value or key not in VALID_PLATFORMS else value)


def normalize_url(value: str, platform: str = "") -> str:
    value = clean(value)
    if not value:
        return ""
    if value.startswith("@") and platform in {"TikTok", "Instagram"}:
        host = "tiktok.com" if platform == "TikTok" else "instagram.com"
        value = f"https://{host}/{value}"
    elif not re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I):
        value = "https://" + value
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold()
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+", "/", parsed.path).rstrip("/") or "/"
    if platform == "Instagram" and path.startswith("/@"):
        path = "/" + path[2:]
    elif platform == "TikTok" and path.startswith("/") and not path.startswith("/@"):
        path = "/@" + path[1:]
    return urlunsplit(("https", host, path, "", ""))


def identity(platform: str, profile_url: str) -> str:
    return f"{platform} | {normalize_url(profile_url)}"


def parse_number(value: str) -> float | int | None:
    value = clean(value).replace(",", "")
    if not value:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    if not match:
        return None
    number = float(match.group(0))
    return int(number) if number.is_integer() else number


def parse_tags(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，、;；|/]", value) if item.strip()]


def read_rows(path: Path) -> list[tuple[int, dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
        reader = csv.DictReader(handle, dialect=dialect)
        return [(line_number, header_map(row)) for line_number, row in enumerate(reader, start=2)]


@dataclass
class Creator:
    source_line: int
    name: str
    platform: str
    profile_url: str
    followers: float | int | None
    email: str
    source: str
    tags: list[str]
    cpm: float | int | None
    quote: float | int | None
    currency: str

    @property
    def key(self) -> str:
        return identity(self.platform, self.profile_url)

    def fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "达人昵称": self.name,
            "平台": self.platform,
            "主页链接": {"link": self.profile_url, "text": self.profile_url},
            "最新状态": "待导入",
        }
        if self.followers is not None:
            fields["粉丝数量"] = self.followers
        if self.email:
            fields["联系邮箱"] = self.email
            fields["联系方式"] = ["Gmail"]
        else:
            fields["联系方式"] = ["TikTok DM" if self.platform == "TikTok" else "Instagram DM"]
        if self.source:
            fields["来源"] = self.source
        if self.tags:
            fields["达人标签"] = self.tags
        if self.cpm is not None:
            fields["CPM"] = self.cpm
        if self.quote is not None:
            fields["首次报价"] = self.quote
        if self.currency:
            fields["币种"] = self.currency
        return fields


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str) -> None:
        response = requests.post(
            f"{API_ROOT}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu authentication failed: {data}")
        self.headers = {
            "Authorization": f"Bearer {data['tenant_access_token']}",
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = requests.request(
            method,
            f"{API_ROOT}{path}",
            headers=self.headers,
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu API {method} {path} failed: {data}")
        return data["data"]

    def list_records(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page_token = ""
        while True:
            suffix = "?page_size=500"
            if page_token:
                suffix += f"&page_token={page_token}"
            data = self.request("GET", f"/bitable/v1/apps/{app_token}/tables/{table_id}/records{suffix}")
            records.extend(data.get("items", []))
            if not data.get("has_more"):
                return records
            page_token = data.get("page_token", "")
            if not page_token:
                raise RuntimeError("Feishu returned has_more without a page_token.")

    def batch_create(self, app_token: str, table_id: str, creators: list[Creator]) -> list[dict[str, Any]]:
        created: list[dict[str, Any]] = []
        for start in range(0, len(creators), BATCH_SIZE):
            batch = creators[start : start + BATCH_SIZE]
            data = self.request(
                "POST",
                f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
                {"records": [{"fields": creator.fields()} for creator in batch]},
            )
            created.extend(data.get("records", []))
        return created


def require_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("status") != "created":
        raise ValueError("Manifest is not a completed creation manifest.")
    app_token = manifest.get("app", {}).get("app_token")
    table_id = manifest.get("table_ids", {}).get("达人主档")
    if not app_token or not table_id:
        raise ValueError("Manifest must contain app.app_token and table_ids.达人主档.")
    return manifest


def normalize_creators(rows: Iterable[tuple[int, dict[str, str]]]) -> tuple[list[Creator], list[dict[str, Any]]]:
    creators: list[Creator] = []
    skipped: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for line, row in rows:
        name = clean(row.get("name"))
        platform = normalize_platform(row.get("platform", ""))
        profile_url = normalize_url(row.get("profile_url", ""), platform)
        if not name or not profile_url:
            skipped.append({"line": line, "reason": "missing_name_or_profile_url", "name": name, "profile_url": profile_url})
            continue
        creator = Creator(
            source_line=line,
            name=name,
            platform=platform,
            profile_url=profile_url,
            followers=parse_number(row.get("followers", "")),
            email=clean(row.get("email")),
            source=clean(row.get("source")),
            tags=parse_tags(row.get("tags", "")),
            cpm=parse_number(row.get("cpm", "")),
            quote=parse_number(row.get("quote", "")),
            currency=clean(row.get("currency", "")),
        )
        if creator.key in seen:
            skipped.append({"line": line, "reason": "duplicate_in_input", "key": creator.key, "first_line": seen[creator.key]})
            continue
        seen[creator.key] = line
        creators.append(creator)
    return creators, skipped


def existing_keys(records: Iterable[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for record in records:
        fields = record.get("fields", {})
        platform = normalize_platform(clean(fields.get("平台")))
        profile_value = fields.get("主页链接", "")
        if isinstance(profile_value, dict):
            profile_value = profile_value.get("link") or profile_value.get("text") or ""
        if platform and profile_value:
            keys.add(identity(platform, clean(profile_value)))
    return keys


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path, help="CSV or TSV creator list")
    parser.add_argument("--report", type=Path, help="JSON report path")
    parser.add_argument("--apply", action="store_true", help="Write new creators to Feishu")
    args = parser.parse_args()

    manifest = require_manifest(args.manifest)
    rows = read_rows(args.input)
    creators, skipped = normalize_creators(rows)
    app_token = manifest["app"]["app_token"]
    table_id = manifest["table_ids"]["达人主档"]

    client: FeishuClient | None = None
    if args.apply:
        app_id = os.environ.get("FEISHU_APP_ID")
        app_secret = os.environ.get("FEISHU_APP_SECRET")
        if not app_id or not app_secret:
            raise RuntimeError("Set FEISHU_APP_ID and FEISHU_APP_SECRET before using --apply.")
        client = FeishuClient(app_id, app_secret)
        current_keys = existing_keys(client.list_records(app_token, table_id))
    else:
        current_keys = set()

    new_creators: list[Creator] = []
    for creator in creators:
        if creator.key in current_keys:
            skipped.append({"line": creator.source_line, "reason": "duplicate_in_current_project", "key": creator.key})
        else:
            new_creators.append(creator)
            current_keys.add(creator.key)

    created_records: list[dict[str, Any]] = []
    if args.apply and new_creators:
        assert client is not None
        created_records = client.batch_create(app_token, table_id, new_creators)

    report = {
        "project_name": manifest["project_name"],
        "manifest": str(args.manifest.resolve()),
        "input": str(args.input.resolve()),
        "mode": "apply" if args.apply else "dry_run",
        "candidate_count": len(creators),
        "new_count": len(new_creators),
        "created_count": len(created_records),
        "skipped_count": len(skipped),
        "skipped": skipped,
        "new_creators": [{"line": creator.source_line, "key": creator.key, "name": creator.name} for creator in new_creators],
        "created_record_ids": [record.get("record_id") for record in created_records],
    }
    report_path = args.report or args.input.with_name(f"{args.input.stem}-import-report.json")
    write_report(report_path, report)
    print(json.dumps({key: report[key] for key in ("mode", "candidate_count", "new_count", "created_count", "skipped_count")}, ensure_ascii=False, indent=2))
    print(f"Report written to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
