#!/usr/bin/env python3
"""Read new Gmail messages and match them to the active creator project.

This scanner reads Gmail and the active project's Feishu Bitable. By default it
writes a local JSON report only. With --apply it writes immutable Communication Log
records and updates uniquely matched Creator Master records; it never sends mail.
The scan cursor advances only after all requested writes succeed.

Required environment variables:
  FEISHU_APP_ID
  FEISHU_APP_SECRET

The project manifest must contain app.app_token and table_ids.达人主档.
Use --since on the first run; later runs use --state automatically.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import requests

API_ROOT = "https://open.feishu.cn/open-apis"
MAX_BODY_CHARS = 8000


@dataclass(frozen=True)
class CreatorRef:
    record_id: str
    name: str
    email: str
    thread_id: str
    latest_message_id: str
    profile_url: str
    platform: str
    current_status: str


@dataclass(frozen=True)
class GmailMessage:
    message_id: str
    thread_id: str
    internal_date_ms: int
    sender_name: str
    sender_email: str
    subject: str
    message_id_header: str
    in_reply_to: str
    references: str
    snippet: str
    body: str
    auto_submitted: str

    def report(self, match: dict[str, Any]) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "internal_date_ms": self.internal_date_ms,
            "sender_name": self.sender_name,
            "sender_email": self.sender_email,
            "subject": self.subject,
            "message_id_header": self.message_id_header,
            "in_reply_to": self.in_reply_to,
            "references": self.references,
            "snippet": self.snippet,
            "body": self.body,
            "match": match,
        }


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

    def list_creator_records(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
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
            page = data["data"]
            records.extend(page.get("items", []))
            if not page.get("has_more"):
                return records
            page_token = page.get("page_token", "")
            if not page_token:
                raise RuntimeError("Feishu returned has_more without a page_token.")

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

    def create_communication_record(
        self, app_token: str, table_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        response = requests.post(
            f"{API_ROOT}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            headers=self.headers,
            json={"fields": fields},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu communication create failed: {data}")
        return data.get("data", {}).get("record", {})

    def update_creator_record(
        self, app_token: str, table_id: str, record_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        response = requests.put(
            f"{API_ROOT}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
            headers=self.headers,
            json={"fields": fields},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu creator update failed: {data}")
        return data.get("data", {}).get("record", {})


def run_gws(arguments: list[str]) -> str:
    result = subprocess.run(
        ["gws", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError(f"gws read failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def parse_json_or_ndjson(output: str) -> list[dict[str, Any]]:
    output = output.strip()
    if not output:
        return []
    try:
        parsed = json.loads(output)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        pages: list[dict[str, Any]] = []
        for line in output.splitlines():
            line = line.strip()
            if line:
                pages.append(json.loads(line))
        return pages


def get_own_email() -> tuple[str | None, str | None]:
    """Read the authenticated Gmail address for self-send filtering."""
    try:
        output = run_gws(
            [
                "gmail",
                "users",
                "getProfile",
                "--params",
                json.dumps({"userId": "me"}),
                "--format",
                "json",
            ]
        )
    except Exception as exc:  # noqa: BLE001 - degrade to inbox-only guard
        return None, f"Gmail profile lookup failed: {exc}"
    for page in parse_json_or_ndjson(output):
        email = page.get("emailAddress")
        if email:
            return str(email).casefold(), None
    return None, "Gmail profile response did not include emailAddress."


def list_message_refs(query: str) -> list[dict[str, str]]:
    output = run_gws(
        [
            "gmail",
            "users",
            "messages",
            "list",
            "--params",
            json.dumps({"userId": "me", "q": query, "maxResults": 500}),
            "--page-all",
            "--page-limit",
            "20",
            "--format",
            "json",
        ]
    )
    refs: list[dict[str, str]] = []
    for page in parse_json_or_ndjson(output):
        refs.extend(page.get("messages", []))
    return refs


def dedupe_refs(refs: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    duplicates = 0
    for ref in refs:
        message_id = str(ref.get("id", "")).strip()
        if not message_id or message_id in seen:
            duplicates += 1
            continue
        seen.add(message_id)
        unique.append(ref)
    return unique, duplicates


def get_message(message_id: str) -> dict[str, Any]:
    output = run_gws(
        [
            "gmail",
            "users",
            "messages",
            "get",
            "--params",
            json.dumps({"userId": "me", "id": message_id, "format": "full"}),
            "--format",
            "json",
        ]
    )
    pages = parse_json_or_ndjson(output)
    if len(pages) != 1:
        raise RuntimeError(f"Expected one Gmail message for {message_id}, got {len(pages)}.")
    return pages[0]


def header_map(message: dict[str, Any]) -> dict[str, str]:
    headers = message.get("payload", {}).get("headers", [])
    return {str(item.get("name", "")).casefold(): str(item.get("value", "")) for item in headers}


def decode_body(data: str) -> str:
    if not data:
        return ""
    try:
        decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        return decoded.decode("utf-8", errors="replace")
    except (ValueError, UnicodeError):
        return ""


def body_text(part: dict[str, Any]) -> str:
    mime = part.get("mimeType", "")
    if mime == "text/plain":
        text = decode_body(part.get("body", {}).get("data", ""))
        if text:
            return text
    for child in part.get("parts", []) or []:
        text = body_text(child)
        if text:
            return text
    return ""


def parse_gmail_message(message: dict[str, Any]) -> GmailMessage:
    headers = header_map(message)
    sender_name, sender_email = parseaddr(headers.get("from", ""))
    body = body_text(message.get("payload", {}))
    return GmailMessage(
        message_id=str(message.get("id", "")),
        thread_id=str(message.get("threadId", "")),
        internal_date_ms=int(message.get("internalDate", "0") or 0),
        sender_name=sender_name,
        sender_email=sender_email.casefold(),
        subject=headers.get("subject", ""),
        message_id_header=headers.get("message-id", ""),
        in_reply_to=headers.get("in-reply-to", ""),
        references=headers.get("references", ""),
        snippet=str(message.get("snippet", "")),
        body=body[:MAX_BODY_CHARS],
        auto_submitted=headers.get("auto-submitted", "").casefold(),
    )


def as_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(as_text(item) for item in value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("link") or "")
    return str(value or "").strip()


def creator_refs(records: list[dict[str, Any]]) -> list[CreatorRef]:
    refs: list[CreatorRef] = []
    for record in records:
        fields = record.get("fields", {})
        refs.append(
            CreatorRef(
                record_id=str(record.get("record_id", "")),
                name=as_text(fields.get("达人昵称")),
                email=as_text(fields.get("联系邮箱")).casefold(),
                thread_id=as_text(fields.get("Gmail线程 ID")),
                latest_message_id=as_text(fields.get("最近邮件 Message ID")),
                profile_url=as_text(fields.get("主页链接")),
                platform=as_text(fields.get("平台")),
                current_status=as_text(fields.get("最新状态")),
            )
        )
    return refs


QUOTE_PATTERN = re.compile(
    r"(?:(?:USD|US\$|\$|EUR|GBP|CNY|RMB)\s*([0-9]{2,6}(?:[,.][0-9]{1,2})?)|([0-9]{2,6}(?:[,.][0-9]{1,2})?)\s*(?:USD|EUR|GBP|CNY|RMB))",
    re.IGNORECASE,
)


def parse_quote(message: GmailMessage) -> tuple[float | int | None, str]:
    text = f"{message.subject}\n{message.body}"
    match = QUOTE_PATTERN.search(text)
    if not match:
        return None, ""
    raw = next((value for value in match.groups() if value), "").replace(",", "")
    amount = float(raw)
    if amount.is_integer():
        amount = int(amount)
    token = match.group(0).upper()
    if "$" in token or "USD" in token:
        return amount, "USD"
    if "EUR" in token:
        return amount, "EUR"
    if "GBP" in token:
        return amount, "GBP"
    if "RMB" in token or "CNY" in token:
        return amount, "CNY"
    return amount, "其他"


def classify_message(message: GmailMessage) -> str:
    sender = message.sender_email
    subject = message.subject.casefold()
    text = f"{subject}\n{message.body.casefold()}"
    if message.auto_submitted or "mailer-daemon" in sender or "delivery status" in subject:
        return "自动回复或退信"
    if "out of office" in subject or "automatic reply" in subject:
        return "自动回复或退信"
    if parse_quote(message)[0] is not None or any(
        phrase in text for phrase in ("my rate", "my rates", "budget", "price", "fee", "per video")
    ):
        return "已报价"
    if any(phrase in text for phrase in ("invoice", "payment method", "paypal", "bank transfer")):
        return "付款资料"
    if any(phrase in text for phrase in ("here is the draft", "new draft", "video draft", "attached the video", "published", "live link")):
        return "已交稿"
    if any(phrase in text for phrase in ("signed up", "registered", "registration", "my account")):
        return "已注册"
    if any(phrase in text for phrase in ("not interested", "decline", "can't collaborate", "cannot collaborate", "pass on")):
        return "拒绝"
    if any(phrase in text for phrase in ("interested", "would love to", "happy to", "let's move forward", "love to collaborate")):
        return "未报价有意向"
    return "待人工判断"


def communication_type(classification: str) -> str:
    return {
        "已报价": "询价",
        "未报价有意向": "询价",
        "已注册": "注册",
        "已交稿": "视频反馈",
        "付款资料": "付款资料",
        "拒绝": "其他",
        "自动回复或退信": "其他",
    }.get(classification, "其他")


def creator_status_for(classification: str) -> str | None:
    return {
        "已报价": "已报价待评估",
        "未报价有意向": "待询价",
        "已注册": "待充值",
        "已交稿": "合作执行中",
        "拒绝": "拒绝",
    }.get(classification)


def communication_fields(
    message: GmailMessage,
    match: dict[str, Any],
    classification: str,
    quote: tuple[float | int | None, str],
) -> dict[str, Any]:
    amount, currency = quote
    fields: dict[str, Any] = {
        "沟通记录": message.message_id,
        "关联达人主页键": f"{match.get('platform', '')} | {match.get('profile_url', '')}",
        "沟通时间": message.internal_date_ms,
        "渠道": "Gmail",
        "方向": "收到",
        "沟通类型": communication_type(classification),
        "邮件线程 ID": message.thread_id,
        "邮件 Message ID": message.message_id,
        "收件人邮箱": "",
        "邮件主题": message.subject,
        "内容摘要": message.snippet[:500],
        "完整正文或 DM 文案": message.body,
        "发送审批状态": "不发送",
        "解析结果": {
            "已报价": "已报价",
            "未报价有意向": "未报价有意向",
            "拒绝": "拒绝",
            "已注册": "已注册",
            "已交稿": "已交稿",
            "自动回复或退信": "自动回复",
        }.get(classification, "待人工判断"),
    }
    if amount is not None:
        fields["提取报价"] = amount
        fields["报价币种"] = currency or "其他"
    return fields


def creator_update_fields(
    message: GmailMessage,
    classification: str,
    quote: tuple[float | int | None, str],
) -> dict[str, Any]:
    amount, currency = quote
    fields: dict[str, Any] = {
        "Gmail线程 ID": message.thread_id,
        "最近邮件 Message ID": message.message_id,
        "最近互动时间": message.internal_date_ms,
        "具体进度": message.snippet[:500],
    }
    status = creator_status_for(classification)
    if status:
        fields["最新状态"] = status
    if amount is not None:
        fields["首次报价"] = amount
        if currency:
            fields["币种"] = currency
    return fields


def match_creator(message: GmailMessage, creators: list[CreatorRef]) -> dict[str, Any]:
    by_thread = [creator for creator in creators if creator.thread_id and creator.thread_id == message.thread_id]
    if len(by_thread) == 1:
        creator = by_thread[0]
        return {"status": "unique_thread", "record_id": creator.record_id, "name": creator.name, "profile_url": creator.profile_url, "platform": creator.platform}
    if len(by_thread) > 1:
        return {"status": "ambiguous_thread", "candidate_record_ids": [creator.record_id for creator in by_thread]}

    by_email = [creator for creator in creators if creator.email and creator.email == message.sender_email]
    if len(by_email) == 1:
        creator = by_email[0]
        return {"status": "unique_email", "record_id": creator.record_id, "name": creator.name, "profile_url": creator.profile_url, "platform": creator.platform}
    if len(by_email) > 1:
        return {"status": "ambiguous_email", "candidate_record_ids": [creator.record_id for creator in by_email]}
    return {"status": "unmatched"}


def parse_since(value: str) -> int:
    if value.isdigit():
        return int(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def mark_already_processed(
    message: GmailMessage,
    match: dict[str, Any],
    creators_by_record: dict[str, CreatorRef],
) -> dict[str, Any]:
    if match.get("status") not in {"unique_thread", "unique_email"}:
        return match
    creator = creators_by_record.get(match.get("record_id", ""))
    if not creator or not creator.latest_message_id:
        return match
    seen_ids = {message.message_id.casefold(), message.message_id_header.casefold()}
    if creator.latest_message_id.casefold() in seen_ids:
        marked = dict(match)
        marked["status"] = "already_processed"
        return marked
    return match


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"State file {path} is corrupt; inspect or remove it before rescanning with --since."
        ) from exc


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--since", help="ISO timestamp, YYYY-MM-DD, or epoch milliseconds for the first scan")
    parser.add_argument("--state", type=Path, help="Local cursor file; defaults beside the manifest")
    parser.add_argument("--report", type=Path, help="JSON report path; defaults beside the manifest")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write matched messages to Communication Log and update Creator Master; never sends mail",
    )
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    app_token = manifest.get("app", {}).get("app_token")
    table_id = manifest.get("table_ids", {}).get("达人主档")
    communication_table_id = manifest.get("table_ids", {}).get("沟通记录")
    if manifest.get("status") != "created" or not app_token or not table_id:
        raise ValueError("Manifest must be a completed creation manifest with app/table IDs.")
    if args.apply and not communication_table_id:
        raise ValueError("Manifest must include table_ids.沟通记录 when --apply is used.")

    state_path = args.state or args.manifest.with_name("gmail-scan-state.json")
    state = load_state(state_path)
    since_ms = int(state.get("last_successful_internal_date_ms", 0))
    if not since_ms:
        if not args.since:
            raise ValueError("First scan requires --since; later scans use the local state cursor.")
        since_ms = parse_since(args.since)

    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise RuntimeError("Set FEISHU_APP_ID and FEISHU_APP_SECRET before scanning the project inbox.")

    # Gmail's `after:` is second-granular, so move the lower bound back one second
    # and apply the exact millisecond filter after fetching messages.
    query = f"after:{max(0, since_ms // 1000 - 1)}"
    notes: list[str] = []
    own_email, own_email_note = get_own_email()
    if own_email_note:
        notes.append(own_email_note + " Falling back to inbox-only query.")
        query = f"in:inbox {query}"

    client = FeishuClient(app_id, app_secret)
    creators = creator_refs(client.list_creator_records(app_token, table_id))
    creator_by_id = {creator.record_id: creator for creator in creators}
    existing_communication_ids: set[str] = set()
    if args.apply:
        for record in client.list_records(app_token, communication_table_id):
            message_id = as_text(record.get("fields", {}).get("邮件 Message ID"))
            if message_id:
                existing_communication_ids.add(message_id.casefold())
    refs, duplicate_ref_count = dedupe_refs(list_message_refs(query))
    messages: list[GmailMessage] = []
    self_sent_skipped = 0
    for ref in refs:
        message = parse_gmail_message(get_message(str(ref["id"])))
        if message.internal_date_ms <= since_ms:
            continue
        if own_email and message.sender_email == own_email:
            self_sent_skipped += 1
            continue
        messages.append(message)

    results: list[dict[str, Any]] = []
    for message in sorted(messages, key=lambda item: item.internal_date_ms):
        match = mark_already_processed(message, match_creator(message, creators), creator_by_id)
        classification = classify_message(message)
        quote = parse_quote(message)
        match["classification"] = classification
        match["quote"] = {"amount": quote[0], "currency": quote[1]}
        if (
            message.message_id.casefold() in existing_communication_ids
            or message.message_id_header.casefold() in existing_communication_ids
        ):
            match["status"] = "already_processed"
        match["write_status"] = (
            "already_processed"
            if match["status"] == "already_processed"
            else "pending_apply" if args.apply else "report_only"
        )
        results.append(message.report(match))

    if args.apply:
        for item, message in zip(results, sorted(messages, key=lambda item: item.internal_date_ms)):
            match = item["match"]
            if match["status"] not in {"unique_thread", "unique_email"}:
                match["write_status"] = "manual_review_no_write"
                continue
            if match["write_status"] == "already_processed":
                continue
            creator = creator_by_id[match["record_id"]]
            quote = (match["quote"]["amount"], match["quote"]["currency"])
            client.create_communication_record(
                app_token,
                communication_table_id,
                communication_fields(message, match, match["classification"], quote),
            )
            client.update_creator_record(
                app_token,
                table_id,
                creator.record_id,
                creator_update_fields(message, match["classification"], quote),
            )
            match["write_status"] = "applied"

    max_date = max((message.internal_date_ms for message in messages), default=since_ms)
    new_cursor = max(since_ms, max_date)
    actionable = {"unique_thread", "unique_email"}
    report = {
        "project_name": manifest.get("project_name"),
        "mode": "apply" if args.apply else "read_only",
        "query": query,
        "own_email": own_email,
        "notes": notes,
        "previous_cursor_ms": since_ms,
        "new_cursor_ms": new_cursor,
        "message_count": len(messages),
        "duplicate_ref_count": duplicate_ref_count,
        "self_sent_skipped_count": self_sent_skipped,
        "unique_thread_matches": sum(item["match"]["status"] == "unique_thread" for item in results),
        "unique_email_matches": sum(item["match"]["status"] == "unique_email" for item in results),
        "manual_review_count": sum(item["match"]["status"] not in actionable | {"already_processed"} for item in results),
        "applied_count": sum(item["match"].get("write_status") == "applied" for item in results),
        "already_processed_count": sum(item["match"].get("write_status") == "already_processed" for item in results),
        "messages": results,
    }
    report_path = args.report or args.manifest.with_name("gmail-scan-report.json")
    write_json(report_path, report)
    write_json(
        state_path,
        {
            "project_name": manifest.get("project_name"),
            "last_successful_internal_date_ms": new_cursor,
            "last_successful_scan_at": datetime.now(timezone.utc).isoformat(),
            "last_report": str(report_path.resolve()),
        },
    )
    print(json.dumps({key: report[key] for key in ("mode", "message_count", "unique_thread_matches", "unique_email_matches", "manual_review_count")}, ensure_ascii=False, indent=2))
    print(f"Report written to {report_path}")
    print(f"State written to {state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
