#!/usr/bin/env python3
"""Build a reviewed Gmail first-outreach queue without sending email.

The command reads the active project's Creator Master and a private project config,
then writes a JSON queue for human review. It never creates Gmail drafts and never
sends external messages.

Required environment variables:
  FEISHU_APP_ID
  FEISHU_APP_SECRET
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import requests

from workflow_policy import validate_creator_identity

API_ROOT = "https://open.feishu.cn/open-apis"
ACTIVE_STATUSES = {"待触达", "待审核"}
PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(text(item) for item in value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("link") or "").strip()
    return str(value or "").strip()


def render(template: str, values: dict[str, str]) -> tuple[str, list[str]]:
    missing: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = values.get(key, "")
        if not value:
            missing.add(key)
        return value

    rendered = PLACEHOLDER.sub(replace, template)
    unresolved = sorted(set(PLACEHOLDER.findall(rendered)) | missing)
    return rendered, unresolved


def load_template(path: Path, label: str) -> str:
    if not path.exists() or not path.is_file():
        raise ValueError(f"{label} does not exist: {path}")
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"{label} is empty: {path}")
    return content


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

    def list_records(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page_token = ""
        while True:
            query = "?page_size=500"
            if page_token:
                query += f"&page_token={page_token}"
            response = requests.get(
                f"{API_ROOT}/bitable/v1/apps/{app_token}/tables/{table_id}/records{query}",
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 0:
                raise RuntimeError(f"Feishu record read failed: {data}")
            page = data.get("data", {})
            records.extend(page.get("items", []))
            if not page.get("has_more"):
                return records
            page_token = page.get("page_token", "")
            if not page_token:
                raise RuntimeError("Feishu returned has_more without a page_token.")


def build_queue(
    records: list[dict[str, Any]],
    *,
    project_name: str,
    brand_or_product: str,
    subject_template: str,
    body_template: str,
    attachment_path: str,
) -> dict[str, Any]:
    queue: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_emails: set[str] = set()
    known_creator_names = [
        text(record.get("fields", {}).get("达人昵称"))
        for record in records
        if text(record.get("fields", {}).get("达人昵称"))
    ]

    for record in records:
        fields = record.get("fields", {})
        name = text(fields.get("达人昵称"))
        email = text(fields.get("联系邮箱")).casefold()
        platform = text(fields.get("平台"))
        profile_url = text(fields.get("主页链接"))
        status = text(fields.get("最新状态"))
        thread_id = text(fields.get("Gmail线程 ID"))
        reason = ""
        if not name or not email or "@" not in email:
            reason = "missing_or_invalid_email_or_name"
        elif status not in ACTIVE_STATUSES:
            reason = f"status_not_ready:{status or 'empty'}"
        elif thread_id:
            reason = "existing_gmail_thread_requires_original_thread_review"
        elif email in seen_emails:
            reason = "duplicate_email_in_queue"
        if reason:
            skipped.append({"record_id": record.get("record_id", ""), "name": name, "email": email, "reason": reason})
            continue

        values = {
            "creator_name": name,
            "email": email,
            "platform": platform,
            "profile_url": profile_url,
            "project_name": project_name,
            "brand_or_product": brand_or_product,
        }
        subject, subject_missing = render(subject_template, values)
        body, body_missing = render(body_template, values)
        missing = sorted(set(subject_missing + body_missing))
        if missing:
            skipped.append({
                "record_id": record.get("record_id", ""),
                "name": name,
                "email": email,
                "reason": "missing_template_values",
                "fields": missing,
            })
            continue
        identity_errors = validate_creator_identity(
            record_id=str(record.get("record_id", "")),
            creator_name=name,
            creator_email=email,
            to=email,
            body=body,
            subject=subject,
            platform=platform,
            profile_url=profile_url,
            source_identity={
                "record_id": record.get("record_id", ""),
                "creator_name": name,
                "creator_email": email,
                "platform": platform,
                "profile_url": profile_url,
            },
            known_creator_names=known_creator_names,
        )
        if identity_errors:
            skipped.append({
                "record_id": record.get("record_id", ""),
                "name": name,
                "email": email,
                "reason": "creator_identity_mismatch",
                "errors": identity_errors,
            })
            continue
        queue.append({
            "record_id": record.get("record_id", ""),
            "creator_name": name,
            "creator_email": email,
            "to": email,
            "subject": subject,
            "body": body,
            "attachment_path": attachment_path,
            "platform": platform,
            "profile_url": profile_url,
            "source_identity": {
                "record_id": record.get("record_id", ""),
                "creator_name": name,
                "creator_email": email,
                "platform": platform,
                "profile_url": profile_url,
            },
            "known_creator_names": known_creator_names,
            "approval_status": "待确认",
        })
        seen_emails.add(email)

    return {
        "mode": "review_only",
        "project_name": project_name,
        "queue_count": len(queue),
        "skipped_count": len(skipped),
        "queue": queue,
        "skipped": skipped,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    config = json.loads(args.config.read_text(encoding="utf-8"))
    app_token = manifest.get("app", {}).get("app_token")
    table_id = manifest.get("table_ids", {}).get("达人主档")
    if manifest.get("status") != "created" or not app_token or not table_id:
        raise ValueError("Manifest must be a completed creation manifest with app/table IDs.")

    outreach = config.get("outreach", {})
    config_dir = args.config.parent
    body_path = Path(outreach.get("first_email_template_path", ""))
    if not body_path.is_absolute():
        body_path = config_dir / body_path
    body_template = load_template(body_path, "first email template")
    subject_template = outreach.get("first_email_subject_template", "Collaboration opportunity with {{brand_or_product}}")
    attachment = text(outreach.get("brief_attachment_path"))
    if attachment:
        attachment_path = Path(attachment)
        if not attachment_path.is_absolute():
            attachment_path = config_dir / attachment_path
        if not attachment_path.exists():
            raise ValueError(f"Brief attachment does not exist: {attachment_path}")
        attachment = str(attachment_path.resolve())

    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise RuntimeError("Set FEISHU_APP_ID and FEISHU_APP_SECRET before reading the project Base.")
    client = FeishuClient(app_id, app_secret)
    report = build_queue(
        client.list_records(app_token, table_id),
        project_name=text(config.get("project_name")),
        brand_or_product=text(config.get("brand_or_product")),
        subject_template=subject_template,
        body_template=body_template,
        attachment_path=attachment,
    )
    output = args.output or args.manifest.with_name("gmail-outreach-queue.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("mode", "queue_count", "skipped_count")}, ensure_ascii=False, indent=2))
    print(f"Queue written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
