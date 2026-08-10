#!/usr/bin/env python3
"""Preview or apply first-outreach send results to the active Feishu project.

Default mode validates the result file and prints the records that would be written.
Only --apply creates Communication Log records or updates Creator Master records.
Existing message IDs and creator records with the same sent Message ID are skipped.
This command never sends Gmail messages.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from workflow_policy import validate_creator_identity

API_ROOT = "https://open.feishu.cn/open-apis"


def text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(text(item) for item in value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("link") or "").strip()
    return str(value or "").strip()


def sent_at_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1000)


def creator_key(fields: dict[str, Any]) -> str:
    return f"{text(fields.get('平台'))} | {text(fields.get('主页链接'))}"


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

    def create_record(self, app_token: str, table_id: str, fields: dict[str, Any]) -> dict[str, Any]:
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

    def update_record(
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


def validate_result_file(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if report.get("mode") != "sent":
        errors.append("result_mode_must_be_sent")
    results = report.get("results")
    if not isinstance(results, list) or not results:
        errors.append("result_list_is_empty")
        return errors
    seen: set[str] = set()
    required = ("record_id", "creator_name", "to", "subject", "body", "sent_at", "message_id", "thread_id")
    for index, item in enumerate(results):
        missing = [key for key in required if not text(item.get(key))]
        if missing:
            errors.append(f"result_{index}_missing:" + ",".join(missing))
        message_id = text(item.get("message_id"))
        if text(item.get("status")) != "sent":
            errors.append(f"result_{index}_status_not_sent")
        if message_id and message_id in seen:
            errors.append(f"result_{index}_duplicate_message_id:{message_id}")
        if message_id:
            seen.add(message_id)
        try:
            sent_at_ms(text(item.get("sent_at")))
        except (TypeError, ValueError):
            errors.append(f"result_{index}_invalid_sent_at")
    return errors


def validate_result_creator_identity(
    result: dict[str, Any],
    creator_record_id: str,
    creator_fields: dict[str, Any],
    known_creator_names: list[str],
) -> list[str]:
    """Reject a sent-result whose identity no longer agrees with Creator Master."""

    creator_name = text(creator_fields.get("达人昵称"))
    creator_email = text(creator_fields.get("联系邮箱"))
    result_name = text(result.get("creator_name"))
    result_email = text(result.get("creator_email")) or text(result.get("to"))
    errors: list[str] = []
    if text(result.get("record_id")) != text(creator_record_id):
        errors.append("result_record_id_does_not_match_creator_master")
    if " ".join(result_name.casefold().split()) != " ".join(creator_name.casefold().split()):
        errors.append("result_creator_name_does_not_match_creator_master")
    if " ".join(result_email.casefold().split()) != " ".join(creator_email.casefold().split()):
        errors.append("result_creator_email_does_not_match_creator_master")
    errors.extend(validate_creator_identity(
        record_id=creator_record_id,
        creator_name=creator_name,
        creator_email=creator_email,
        to=text(result.get("to")),
        body=text(result.get("body")),
        subject=text(result.get("subject")),
        platform=text(result.get("platform")),
        profile_url=text(result.get("profile_url")),
        source_identity=result.get("source_identity"),
        known_creator_names=known_creator_names,
    ))
    return errors


def build_fields(result: dict[str, Any], creator_fields: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    key = creator_key(creator_fields)
    communication = {
        "沟通记录": text(result["message_id"]),
        "关联达人主页键": key,
        "沟通时间": sent_at_ms(text(result["sent_at"])),
        "渠道": "Gmail",
        "方向": "发出",
        "沟通类型": "首轮邀约",
        "邮件线程 ID": text(result["thread_id"]),
        "邮件 Message ID": text(result["message_id"]),
        "收件人邮箱": text(result["to"]),
        "邮件主题": text(result["subject"]),
        "内容摘要": text(result["body"])[:500],
        "完整正文或 DM 文案": text(result["body"]),
        "发送审批状态": "已发送",
        "解析结果": "待人工判断",
    }
    # A local path is retained in the send-result file. Feishu attachment fields
    # require an uploaded file token, so do not write a fake attachment object.
    creator_update = {
        "Gmail线程 ID": text(result["thread_id"]),
        "最近邮件 Message ID": text(result["message_id"]),
        "最新状态": "已触达",
        "具体进度": "首轮邮件已发送，等待达人回复",
        "首次触达时间": sent_at_ms(text(result["sent_at"])),
        "最近互动时间": sent_at_ms(text(result["sent_at"])),
    }
    return communication, creator_update


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="Write Feishu records; never sends Gmail")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    report = json.loads(args.result.read_text(encoding="utf-8"))
    errors = validate_result_file(report)
    if errors:
        raise ValueError("Result validation failed: " + "; ".join(errors))

    app_token = manifest.get("app", {}).get("app_token")
    master_table = manifest.get("table_ids", {}).get("达人主档")
    communication_table = manifest.get("table_ids", {}).get("沟通记录")
    if manifest.get("status") != "created" or not app_token or not master_table or not communication_table:
        raise ValueError("Manifest must include the created Base and both required table IDs.")

    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise RuntimeError("Set FEISHU_APP_ID and FEISHU_APP_SECRET before reading or writing the project Base.")
    client = FeishuClient(app_id, app_secret)
    creators = {str(item.get("record_id")): item for item in client.list_records(app_token, master_table)}
    communication_records = client.list_records(app_token, communication_table)
    known_creator_names = [
        text(item.get("fields", {}).get("达人昵称"))
        for item in creators.values()
        if text(item.get("fields", {}).get("达人昵称"))
    ]
    existing_message_ids = {
        text(item.get("fields", {}).get("邮件 Message ID"))
        for item in communication_records
        if text(item.get("fields", {}).get("邮件 Message ID"))
    }

    actions: list[dict[str, Any]] = []
    for result in report["results"]:
        record_id = text(result["record_id"])
        creator = creators.get(record_id)
        if not creator:
            actions.append({"record_id": record_id, "status": "manual_review_creator_not_found"})
            continue
        creator_fields = creator.get("fields", {})
        message_id = text(result["message_id"])
        identity_errors = validate_result_creator_identity(
            result, record_id, creator_fields, known_creator_names
        )
        if identity_errors:
            actions.append({
                "record_id": record_id,
                "message_id": message_id,
                "status": "manual_review_creator_identity_mismatch",
                "errors": identity_errors,
            })
            continue
        if message_id in existing_message_ids:
            actions.append({"record_id": record_id, "message_id": message_id, "status": "already_applied"})
            continue
        communication, creator_update = build_fields(result, creator_fields)
        action = {
            "record_id": record_id,
            "creator_name": text(result["creator_name"]),
            "message_id": message_id,
            "thread_id": text(result["thread_id"]),
            "status": "pending_apply",
            "communication_fields": communication,
            "creator_update": creator_update,
        }
        if args.apply:
            client.create_record(app_token, communication_table, communication)
            client.update_record(app_token, master_table, record_id, creator_update)
            action["status"] = "applied"
        actions.append(action)

    output = args.result.with_name(args.result.stem + "-record-result.json")
    output.write_text(
        json.dumps({"mode": "apply" if args.apply else "review_only", "count": len(actions), "actions": actions}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"mode": "apply" if args.apply else "review_only", "count": len(actions), "result": str(output)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
