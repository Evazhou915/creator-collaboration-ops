#!/usr/bin/env python3
"""Review or send a human-approved Gmail outreach queue.

Default mode prints complete drafts and performs no Gmail write. Sending requires
both --send and every queue item having approval_status=已确认. The command sends
messages as new Gmail threads, records returned IDs, and writes a separate result
file. It never guesses recipients or silently skips unapproved items.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import subprocess
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from workflow_policy import validate_creator_identity


def validate_queue(queue: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    items = queue.get("queue")
    if queue.get("mode") != "review_only":
        errors.append("queue_mode_must_be_review_only")
    if not isinstance(items, list) or not items:
        errors.append("queue_is_empty")
        return errors
    seen: set[str] = set()
    required = ("record_id", "creator_name", "to", "subject", "body")
    for index, item in enumerate(items):
        prefix = f"item_{index}"
        missing = [key for key in required if not str(item.get(key, "")).strip()]
        if missing:
            errors.append(f"{prefix}_missing:" + ",".join(missing))
        recipient = str(item.get("to", "")).strip().casefold()
        if recipient and ("@" not in recipient or recipient in seen):
            errors.append(f"{prefix}_invalid_or_duplicate_recipient")
        if recipient:
            seen.add(recipient)
        if "thread_id" in item and item.get("thread_id"):
            errors.append(f"{prefix}_must_not_reuse_existing_thread_for_first_outreach")
        identity_errors = validate_creator_identity(
            record_id=str(item.get("record_id", "")),
            creator_name=str(item.get("creator_name", "")),
            creator_email=str(item.get("creator_email") or item.get("to", "")),
            to=str(item.get("to", "")),
            body=str(item.get("body", "")),
            subject=str(item.get("subject", "")),
            platform=str(item.get("platform", "")),
            profile_url=str(item.get("profile_url", "")),
            source_identity=item.get("source_identity"),
            known_creator_names=item.get("known_creator_names", queue.get("known_creator_names", [])),
        )
        errors.extend(f"{prefix}_{error}" for error in identity_errors)
    return errors


def approved_items(queue: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    errors = validate_queue(queue)
    if errors:
        return [], errors
    items = queue["queue"]
    unapproved = [
        str(item.get("creator_name") or item.get("to") or f"item_{index}")
        for index, item in enumerate(items)
        if item.get("approval_status") != "已确认"
    ]
    if unapproved:
        return [], ["unapproved_items:" + ",".join(unapproved)]
    return items, []


def build_message(item: dict[str, Any]) -> EmailMessage:
    message = EmailMessage()
    message["To"] = str(item["to"]).strip()
    message["Subject"] = str(item["subject"]).strip()
    message.set_content(str(item["body"]).strip())
    attachment_path = str(item.get("attachment_path", "")).strip()
    if attachment_path:
        path = Path(attachment_path)
        if not path.is_file():
            raise ValueError(f"attachment_missing:{path}")
        content_type, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (content_type or "application/octet-stream").split("/", 1)
        message.add_attachment(
            path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
        )
    return message


def encode_raw(message: EmailMessage) -> str:
    return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")


def send_raw(raw: str) -> dict[str, Any]:
    encoded = json.dumps({"raw": raw}, ensure_ascii=False)
    result = subprocess.run(
        ["gws", "gmail", "users", "messages", "send", "--params", '{"userId":"me"}', "--json", encoded],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError(f"gmail_send_failed:{result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gmail_send_invalid_response:{result.stdout.strip()}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--send", action="store_true", help="Send only after every item is marked 已确认")
    args = parser.parse_args()

    queue = json.loads(args.queue.read_text(encoding="utf-8"))
    if not args.send:
        errors = validate_queue(queue)
        if errors:
            raise ValueError("Queue validation failed: " + "; ".join(errors))
        print(json.dumps({"mode": "review_only", "queue_count": len(queue["queue"])}, ensure_ascii=False, indent=2))
        for index, item in enumerate(queue["queue"], start=1):
            print(f"\n--- Draft {index}: {item['creator_name']} <{item['to']}> ---")
            print(f"Subject: {item['subject']}")
            print(item["body"])
            if item.get("attachment_path"):
                print(f"Attachment: {item['attachment_path']}")
        return 0

    items, errors = approved_items(queue)
    if errors:
        raise ValueError("Send blocked: " + "; ".join(errors))
    results: list[dict[str, Any]] = []
    for item in items:
        response = send_raw(encode_raw(build_message(item)))
        results.append({
            "record_id": item["record_id"],
            "creator_name": item["creator_name"],
            "creator_email": item.get("creator_email", item["to"]),
            "to": item["to"],
            "subject": item["subject"],
            "body": item["body"],
            "platform": item.get("platform", ""),
            "profile_url": item.get("profile_url", ""),
            "source_identity": item.get("source_identity"),
            "known_creator_names": item.get("known_creator_names", []),
            "attachment_path": item.get("attachment_path", ""),
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "status": "sent",
            "message_id": response.get("id", ""),
            "thread_id": response.get("threadId", ""),
        })

    output = args.result or args.queue.with_name(args.queue.stem + "-send-result.json")
    output.write_text(
        json.dumps({"mode": "sent", "count": len(results), "results": results}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"mode": "sent", "count": len(results), "result": str(output)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
