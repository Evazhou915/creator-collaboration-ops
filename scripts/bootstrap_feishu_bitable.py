#!/usr/bin/env python3
"""Create a project-isolated Feishu Bitable for creator collaboration operations.

The script is deliberately apply-gated: use --dry-run to inspect the schema, then
pass --apply only when creating a new project. It never modifies an existing base.

Required environment variables:
  FEISHU_APP_ID
  FEISHU_APP_SECRET
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

API_ROOT = "https://open.feishu.cn/open-apis"

TEXT = 1
NUMBER = 2
SINGLE_SELECT = 3
MULTI_SELECT = 4
DATE_TIME = 5
URL = 15
ATTACHMENT = 17


@dataclass(frozen=True)
class Field:
    name: str
    field_type: int
    options: tuple[str, ...] = ()
    primary: bool = False

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "field_name": self.name,
            "type": self.field_type,
        }
        if self.primary:
            payload["is_primary"] = True
        if self.options:
            payload["property"] = {
                "options": [{"name": option, "color": 0} for option in self.options]
            }
        return payload


def text(name: str, *, primary: bool = False) -> Field:
    return Field(name, TEXT, primary=primary)


def number(name: str) -> Field:
    return Field(name, NUMBER)


def single(name: str, *options: str) -> Field:
    return Field(name, SINGLE_SELECT, options)


def multi(name: str, *options: str) -> Field:
    return Field(name, MULTI_SELECT, options)


def date(name: str) -> Field:
    return Field(name, DATE_TIME)


def url(name: str) -> Field:
    return Field(name, URL)


def attachment(name: str) -> Field:
    return Field(name, ATTACHMENT)


CREATOR_FIELDS = (
    text("达人昵称", primary=True),
    single(
        "平台", "TikTok", "Instagram", "YouTube", "X", "其他"
    ),
    url("主页链接"),
    number("粉丝数量"),
    multi("联系方式", "Gmail", "TikTok DM", "Instagram DM", "经纪人", "其他"),
    text("联系邮箱"),
    text("Gmail线程 ID"),
    text("最近邮件 Message ID"),
    text("来源"),
    multi("达人标签", "AI", "学习", "效率", "职场", "创作者", "其他"),
    number("CPM"),
    number("首次报价"),
    single("币种", "USD", "GBP", "EUR", "CNY", "其他"),
    number("第一轮议价目标"),
    number("第一轮议价结果"),
    number("第二轮议价目标"),
    number("第二轮议价结果"),
    number("最终合作价格"),
    text("合作内容"),
    single(
        "最新状态",
        "待导入",
        "待审核",
        "待触达",
        "已触达",
        "已回复待归类",
        "待询价",
        "已报价待评估",
        "议价中",
        "合作待确认",
        "待达人确认合作",
        "待注册",
        "待充值",
        "合作执行中",
        "待发布完成",
        "待收款资料与 Invoice",
        "付款申请待确认",
        "付款审核中",
        "待付款",
        "已付款",
        "已完成",
        "拒绝",
        "报价过高",
        "账号异常",
        "失联",
        "终止合作",
    ),
    text("具体进度"),
    date("首次触达时间"),
    date("最近互动时间"),
    text("负责人"),
    text("风险备注"),
)

COMMUNICATION_FIELDS = (
    text("沟通记录", primary=True),
    text("关联达人主页键"),
    date("沟通时间"),
    single("渠道", "Gmail", "TikTok DM", "Instagram DM", "经纪人", "其他"),
    single("方向", "收到", "发出"),
    single(
        "沟通类型",
        "首轮邀约",
        "询价",
        "议价 1",
        "议价 2",
        "合作确认",
        "注册",
        "充值后推进",
        "脚本反馈",
        "视频反馈",
        "发布",
        "付款资料",
        "其他",
    ),
    text("邮件线程 ID"),
    text("邮件 Message ID"),
    text("收件人邮箱"),
    text("邮件主题"),
    text("内容摘要"),
    text("完整正文或 DM 文案"),
    attachment("附件"),
    single("发送审批状态", "待确认", "已确认", "已发送", "发送失败", "不发送"),
    single(
        "解析结果",
        "已报价",
        "未报价有意向",
        "拒绝",
        "已注册",
        "已交稿",
        "自动回复",
        "待人工判断",
    ),
    number("提取报价"),
    single("报价币种", "USD", "GBP", "EUR", "CNY", "其他"),
    date("下次跟进时间"),
    text("执行异常"),
)

DELIVERABLE_FIELDS = (
    text("交付物名称", primary=True),
    text("关联达人主页键"),
    single("内容形式", "短视频", "长视频", "图文", "其他"),
    single("主发布平台", "TikTok", "Instagram", "YouTube", "X", "其他"),
    multi("交付平台", "TikTok", "Instagram", "YouTube", "X", "其他"),
    single("跨发", "否", "是"),
    single(
        "内容状态",
        "待脚本",
        "脚本审核中",
        "脚本需修改",
        "脚本已通过",
        "待一稿",
        "一稿审核中",
        "待二稿",
        "二稿审核中",
        "待终稿",
        "终稿审核中",
        "待文案审核",
        "待发布",
        "部分发布",
        "已发布",
        "异常",
    ),
    text("内容负责人"),
    attachment("脚本"),
    date("脚本收到时间"),
    date("审核截止时间"),
    text("脚本审核意见"),
    single("脚本审核结论", "待审核", "通过", "需修改"),
    number("脚本修改轮次"),
    number("脚本剩余免费修改次数"),
    attachment("脚本终版"),
    date("脚本通过时间"),
    attachment("一稿"),
    date("一稿收到时间"),
    text("一稿审核意见"),
    single("一稿审核结论", "待审核", "通过", "需修改"),
    attachment("二稿"),
    date("二稿收到时间"),
    text("二稿审核意见"),
    single("二稿审核结论", "待审核", "通过", "需修改"),
    attachment("终稿"),
    date("终稿收到时间"),
    text("终稿审核意见"),
    single("终稿审核结论", "待审核", "通过", "需修改"),
    number("视频修改轮次"),
    number("视频剩余免费修改次数"),
    date("终稿通过时间"),
    text("发布文案"),
    text("发布文案审核意见"),
    single("发布文案审核结论", "待审核", "通过", "需修改"),
    date("文案通过时间"),
    date("计划发布时间"),
    date("实际发布时间"),
    text("各平台发布链接"),
    multi("已发布平台", "TikTok", "Instagram", "YouTube", "X", "其他"),
    text("发布完成度"),
    attachment("发布证据"),
    text("审核记录"),
    text("最新审核摘要"),
    text("异常备注"),
)

FINANCE_FIELDS = (
    text("财务记录", primary=True),
    text("关联达人主页键"),
    text("关联交付物名称"),
    single("财务类型", "充值", "创作者服务费", "其他"),
    single(
        "财务状态",
        "待注册信息",
        "待充值",
        "已充值",
        "待发布完成",
        "待收款资料与 Invoice",
        "Invoice 审核中",
        "付款申请待确认",
        "付款审核中",
        "待付款",
        "已付款",
        "异常",
    ),
    text("注册邮箱"),
    number("充值额度"),
    single("充值单位", "Credits", "USD", "其他"),
    date("充值完成时间"),
    attachment("充值凭据"),
    number("应付金额"),
    single("币种", "USD", "GBP", "EUR", "CNY", "其他"),
    text("收款人法定名称"),
    single("收款方式", "PayPal", "银行转账", "Wise", "其他"),
    text("收款账户"),
    text("Invoice 编号"),
    attachment("Invoice 文件"),
    single("Invoice 核验结果", "待核验", "通过", "金额不符", "信息不全", "重复"),
    text("内部付款申请沟通记录"),
    date("付款申请发送时间"),
    date("实际付款时间"),
    attachment("付款凭证"),
    text("财务负责人"),
    text("异常说明"),
)

TABLES = {
    "达人主档": CREATOR_FIELDS,
    "沟通记录": COMMUNICATION_FIELDS,
    "内容交付": DELIVERABLE_FIELDS,
    "财务": FINANCE_FIELDS,
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

    def create_base(self, name: str) -> dict[str, Any]:
        return self.request("POST", "/bitable/v1/apps", {"name": name})["app"]

    def rename_table(self, app_token: str, table_id: str, name: str) -> None:
        self.request("PATCH", f"/bitable/v1/apps/{app_token}/tables/{table_id}", {"name": name})

    def list_fields(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields").get("items", [])

    def update_field(self, app_token: str, table_id: str, field_id: str, field: Field) -> None:
        self.request(
            "PUT",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{field_id}",
            field.payload(),
        )

    def create_field(self, app_token: str, table_id: str, field: Field) -> None:
        self.request(
            "POST",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            field.payload(),
        )

    def create_table(self, app_token: str, name: str, fields: tuple[Field, ...]) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/bitable/v1/apps/{app_token}/tables",
            {
                "table": {
                    "name": name,
                    "default_view_name": "全部记录",
                    "fields": [field.payload() for field in fields],
                }
            },
        )


def require_string(config: dict[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Configuration requires a non-empty {key!r}.")
    return value.strip()


def describe_plan(project_name: str) -> None:
    print(f"Project: {project_name}")
    for table_name, fields in TABLES.items():
        print(f"- {table_name}: {len(fields)} fields")


def configure_default_creator_table(client: FeishuClient, app_token: str, table_id: str) -> None:
    client.rename_table(app_token, table_id, "达人主档")
    current_fields = client.list_fields(app_token, table_id)
    by_type: dict[int, list[dict[str, Any]]] = {}
    for field in current_fields:
        by_type.setdefault(field["type"], []).append(field)

    # A newly created Bitable has starter text/select/date/attachment fields. Reuse them
    # so the default primary field remains valid, then append the rest of the schema.
    reused: dict[str, str] = {}
    starter_mapping = (
        (TEXT, CREATOR_FIELDS[0]),
        (SINGLE_SELECT, next(field for field in CREATOR_FIELDS if field.name == "最新状态")),
        (DATE_TIME, next(field for field in CREATOR_FIELDS if field.name == "首次触达时间")),
    )
    for field_type, desired in starter_mapping:
        candidates = by_type.get(field_type, [])
        if not candidates:
            continue
        target = candidates.pop(0)
        client.update_field(app_token, table_id, target["field_id"], desired)
        reused[desired.name] = target["field_id"]

    for field in CREATOR_FIELDS:
        if field.name not in reused:
            client.create_field(app_token, table_id, field)


def write_manifest(output_dir: Path, manifest: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "feishu-project-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="Project configuration JSON")
    parser.add_argument("--output-dir", type=Path, help="Directory for the created-base manifest")
    parser.add_argument("--apply", action="store_true", help="Create the Bitable; omit for a dry run")
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    project_name = require_string(config, "project_name")
    output_dir = args.output_dir or args.config.parent
    describe_plan(project_name)

    if not args.apply:
        print("Dry run only. Add --apply to create a new Feishu Bitable.")
        return 0

    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise RuntimeError("Set FEISHU_APP_ID and FEISHU_APP_SECRET before using --apply.")

    client = FeishuClient(app_id, app_secret)
    app = client.create_base(f"{project_name} - 达人合作")
    app_token = app["app_token"]
    table_ids: dict[str, str] = {"达人主档": app["default_table_id"]}

    try:
        configure_default_creator_table(client, app_token, table_ids["达人主档"])
        for table_name in ("沟通记录", "内容交付", "财务"):
            created = client.create_table(app_token, table_name, TABLES[table_name])
            table_ids[table_name] = created["table_id"]
    except Exception:
        manifest_path = write_manifest(
            output_dir,
            {
                "project_name": project_name,
                "status": "partial_failure",
                "app": app,
                "table_ids": table_ids,
                "note": "The created base was intentionally preserved for recovery. Inspect it before retrying.",
            },
        )
        print(f"Partial manifest written to {manifest_path}", file=sys.stderr)
        raise

    manifest = {
        "project_name": project_name,
        "status": "created",
        "app": app,
        "table_ids": table_ids,
        "identity_key": "平台 + 主页链接",
        "relationship_key": "关联达人主页键",
        "config_path": str(args.config.resolve()),
    }
    manifest_path = write_manifest(output_dir, manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"Manifest written to {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
