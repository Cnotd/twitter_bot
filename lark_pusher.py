"""
Owly 飞书推送模块
==================
按"内部情报输出结构"发送格式化卡片消息
结构：一句话结论 → 今日要闻 → 生态项目新闻 → 数据与市场信号 → 值得继续跟踪 → 数据获取状态
"""

import asyncio
import hashlib
import hmac
import base64
import time
from typing import List, Dict, Optional
from dataclasses import dataclass
from loguru import logger

import aiohttp

from tw_config import tw_config, LarkConfig, Importance, SourceLevel
from scoring_engine import ScoredTweet, IntelReportBuilder


class Colors:
    RED = "red"
    GREEN = "green"
    BLUE = "blue"
    YELLOW = "yellow"
    PURPLE = "purple"
    GREY = "grey"
    ORANGE = "orange"


class LarkPusher:
    """飞书消息推送器"""

    def __init__(self, config: LarkConfig = None):
        self.cfg = config or tw_config.lark
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"Content-Type": "application/json"},
        )

    async def stop(self):
        if self.session:
            await self.session.close()
            self.session = None

    # ==================== 主推送入口 ====================
    async def push_report(
        self,
        report: Dict,
        dry_run: bool = False,
    ) -> Dict:
        """推送完整情报报告"""
        results = {"sent": 0, "skipped": 0, "errors": 0, "details": []}
        
        webhook = self.cfg.webhook_url
        if not webhook:
            logger.error("未配置飞书 Webhook")
            return results
        
        # S/A 级事件逐条推送
        urgent_items = []
        key_events = report.get("key_events", [])
        for event in key_events:
            imp = event.get("importance", "")
            if "S" in imp:
                urgent_items.append(event)
        
        for event in urgent_items:
            success = await self._send_event_card(
                event, webhook, is_urgent=True, dry_run=dry_run
            )
            self._record(results, success, event)
        
        # 发送汇总卡片
        success = await self._send_summary_card(report, webhook, dry_run=dry_run)
        self._record(results, success, {"type": "summary"})
        
        if not dry_run:
            logger.info(
                f"推送完成: 发送{results['sent']}条, "
                f"错误{results['errors']}条"
            )
        
        return results

    # ==================== 事件卡片 ====================
    async def _send_event_card(
        self, event: Dict, webhook: str, is_urgent: bool = False, dry_run: bool = False,
    ) -> bool:
        """发送单条事件卡片"""
        color = Colors.RED if is_urgent else Colors.BLUE
        alert = "<at id=all></at> " if is_urgent else ""
        
        facts = event.get("facts", "")
        why = event.get("why_matters", "")
        accounts = ", ".join(event.get("accounts_involved", []))
        source_url = event.get("source_url", "")
        source_level = event.get("source_level", "")
        
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"{'🚨' if is_urgent else '📢'} {event.get('importance', '')} 情报"
                },
                "template": color,
            },
            "elements": [
                # 标题
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"{alert}**{event.get('title', '')}**"
                    }
                },
                {"tag": "hr"},
                # 已确认事实
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"📋 **已确认事实**\n{facts}"
                    }
                },
                {"tag": "hr"},
                # 为什么重要
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"💡 **为什么重要**\n{why}"
                    }
                },
                {"tag": "hr"},
                # 来源信息
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"👤 **涉及账号**: {accounts}\n"
                            f"📊 **来源级别**: {source_level}"
                        )
                    }
                },
                {"tag": "hr"},
                # 操作按钮
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "🔗 查看原帖"},
                            "type": "primary",
                            "url": source_url,
                        }
                    ]
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "🦉 Owly X 情报系统 · 自动采集与分级 · 请交叉核验重要信息"
                        }
                    ]
                }
            ]
        }

        if dry_run:
            logger.info(f"[DRY RUN] Card: {event.get('title', '')[:60]}...")
            return True

        payload = {"msg_type": "interactive", "card": card}
        if self.cfg.webhook_secret:
            payload = self._sign(payload)
        return await self._post(webhook, payload)

    # ==================== 汇总卡片 ====================
    async def _send_summary_card(
        self, report: Dict, webhook: str, dry_run: bool = False,
    ) -> bool:
        """发送情报汇总卡片"""
        summary = report.get("summary", {})
        conclusion = report.get("conclusion", "无更新")
        key_events = report.get("key_events", [])
        eco_news = report.get("eco_news", [])
        data_signals = report.get("data_signals", [])
        watchlist = report.get("watchlist", [])
        data_status = report.get("data_status", {})
        scan_window = report.get("scan_window", {})
        
        elements = []

        # 头部：结论
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**📌 一句话结论**\n{conclusion}"
            }
        })
        elements.append({"tag": "hr"})

        # 扫描时间窗口
        start = scan_window.get("start", "")[:16]
        end = scan_window.get("end", "")[:16]
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"⏱ 扫描窗口: {start} → {end}\n"
                    f"📊 共扫描 **{summary.get('total_scanned', 0)}** 条 | "
                    f"S级 {summary.get('s_count', 0)} | "
                    f"A级 {summary.get('a_count', 0)} | "
                    f"B级 {summary.get('b_count', 0)}"
                )
            }
        })
        elements.append({"tag": "hr"})

        # 今日要闻
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**📰 今日要闻**"
            }
        })
        if key_events:
            for i, event in enumerate(key_events[:10]):
                title = event.get("title", "")[:80]
                imp = event.get("importance", "")
                url = event.get("source_url", "")
                elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**{imp}** {title}\n"
                            f"[查看原帖]({url})"
                        )
                    }
                })
                if i < len(key_events) - 1:
                    elements.append({"tag": "hr"})
        else:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "无 S/A 级要闻"}
            })
        elements.append({"tag": "hr"})

        # 生态项目新闻
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**🏗 生态项目新闻**"
            }
        })
        if eco_news:
            for i, news in enumerate(eco_news[:5]):
                elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"- {news.get('title', '')[:80]}\n"
                            f"  {news.get('accounts_involved', [])}"
                        )
                    }
                })
        else:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "无生态项目更新"}
            })
        elements.append({"tag": "hr"})

        # 数据与市场信号
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**📊 数据与市场信号**"
            }
        })
        if data_signals:
            for sig in data_signals[:3]:
                elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"- {sig.get('title', '')[:80]}"
                    }
                })
        else:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "无可核实数据信号"}
            })
        elements.append({"tag": "hr"})

        # 值得继续跟踪
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**🔍 值得继续跟踪**"
            }
        })
        if watchlist:
            for item in watchlist:
                elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"- {item.get('item', '')[:60]}\n"
                            f"  触发条件: {item.get('trigger', '')[:60]}"
                        )
                    }
                })
        else:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "无待跟踪项"}
            })
        elements.append({"tag": "hr"})

        # 数据获取状态
        ds = data_status
        access = ds.get("x_access", "N/A")
        fallback = "是" if ds.get("fallback_used") else "否"
        sufficient = "是" if ds.get("data_sufficient") else "否"
        checked = ds.get("accounts_checked", 0)
        
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**⚙ 数据获取状态**\n"
                    f"X 访问: {access} | 降级: {fallback} | "
                    f"数据充足: {sufficient} | 已查账号: {checked}"
                )
            }
        })

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "{title} - {time}".format(
                        title=self.cfg.card_title,
                        time=report.get("report_time", "")[:16]
                    )
                },
                "template": Colors.BLUE,
            },
            "elements": elements,
        }

        if dry_run:
            logger.info(f"[DRY RUN] Summary card with {len(elements)} elements")
            return True

        payload = {"msg_type": "interactive", "card": card}
        if self.cfg.webhook_secret:
            payload = self._sign(payload)
        return await self._post(webhook, payload)

    # ==================== 每日日报推送 ====================
    async def push_daily_report(
        self,
        report: str,
        window_label: str = "",
        stats: Dict = None,
        dry_run: bool = False,
    ) -> Dict:
        """
        推送 Grok 生成的每日情报日报
        长文 Markdown → 分割后逐段发送到飞书群
        """
        webhook = self.cfg.webhook_url
        if not webhook:
            logger.error("未配置飞书 Webhook，无法推送日报")
            return {"ok": False, "reason": "no webhook"}

        stats = stats or {}
        result = {"ok": True, "parts": 0, "errors": 0}

        if dry_run:
            logger.info(f"[DRY RUN] 日报: {len(report)} 字符, 窗口: {window_label}")
            return result

        # 先发概览卡片
        overview_card = self._build_daily_overview_card(
            report=report,
            window_label=window_label,
            stats=stats,
        )
        ok = await self._post(webhook, {"msg_type": "interactive", "card": overview_card})
        if ok:
            result["parts"] += 1
        else:
            result["errors"] += 1

        # 长文分割发送（每个 text 消息最多 2000 字符）
        chunks = self._split_markdown(report, max_chars=1800)
        for i, chunk in enumerate(chunks):
            prefix = f"[{i + 1}/{len(chunks)}] " if len(chunks) > 1 else ""
            payload = {
                "msg_type": "text",
                "content": {"text": prefix + chunk},
            }
            ok = await self._post(webhook, payload)
            if ok:
                result["parts"] += 1
            else:
                result["errors"] += 1
            # 分段发送间隔，避免频率限制
            await asyncio.sleep(0.5)

        logger.info(f"日报推送: {result['parts']}段, 错误{result['errors']}")
        return result

    def _build_daily_overview_card(
        self, report: str, window_label: str, stats: Dict = None,
    ) -> Dict:
        """构建日报概览卡片"""
        stats = stats or {}
        s_count = stats.get("s_count", 0)
        a_count = stats.get("a_count", 0)
        b_count = stats.get("b_count", 0)
        total = stats.get("total_tweets", 0)

        # 提取一句结论(第一个 ### 之后的段落)
        conclusion = "详见下文"
        if "###" in report:
            for part in report.split("###"):
                part = part.strip()
                if "一句话结论" in part or "一句结论" in part:
                    lines = part.split("\n", 1)
                    if len(lines) > 1:
                        conclusion = lines[1].strip()[:200]
                    break

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "🦉 Hyperliquid X 每日情报"
                },
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**📌 覆盖窗口**: {window_label}\n**📊 统计**: {total}条采集 | S:{s_count} A:{a_count} B:{b_count} | Grok 生成"
                    }
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**💡 一句话结论**\n{conclusion}"
                    }
                },
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "🦉 Owly X 情报系统 · 每日自动生成 · 请交叉核验重要信息 | 全文见后续消息"
                        }
                    ]
                }
            ]
        }

    def _split_markdown(self, text: str, max_chars: int = 1800) -> List[str]:
        """按自然段落分割长文本"""
        chunks = []
        current = ""

        for line in text.split("\n"):
            if len(current) + len(line) + 1 > max_chars and current:
                chunks.append(current.strip())
                current = line + "\n"
            else:
                current += line + "\n"

        if current.strip():
            chunks.append(current.strip())

        return chunks

    # ==================== 工具方法 ====================
    def _sign(self, payload: Dict) -> Dict:
        secret = self.cfg.webhook_secret
        if not secret:
            return payload
        timestamp = str(int(time.time()))
        sign_str = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            secret.encode("utf-8"), sign_str.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = base64.b64encode(hmac_code).decode("utf-8")
        payload["timestamp"] = timestamp
        payload["sign"] = sign
        return payload

    def _record(self, results: Dict, success: bool, event: Dict = None):
        if success:
            results["sent"] += 1
        else:
            results["errors"] += 1

    async def _post(self, url: str, payload: Dict) -> bool:
        for attempt in range(3):
            try:
                async with self.session.post(url, json=payload) as resp:
                    body = await resp.json()
                    if body.get("code") == 0 or body.get("StatusCode") == 0:
                        return True
                    logger.error(
                        f"Lark API error: {body.get('code')} - {body.get('msg', '')}"
                    )
                    if attempt < 2:
                        await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Lark push failed: {e}")
                if attempt < 2:
                    await asyncio.sleep(1)
        return False


async def test():
    """测试飞书推送（需要真实 webhook）"""
    import sys
    
    # 检查 webhook 是否配置
    if not tw_config.lark.webhook_url:
        print("未配置 Lark Webhook，跳过推送测试")
        print("请在 .env 中设置 LARK_WEBHOOK_URL")
        return
    
    pusher = LarkPusher()
    await pusher.start()
    
    try:
        # 快速自检
        ok = await pusher._post(
            tw_config.lark.webhook_url,
            {
                "msg_type": "text",
                "content": {"text": "🦉 Owly 情报系统连通性测试 - 成功！"}
            }
        )
        if ok:
            print("飞书连通测试成功")
        else:
            print("飞书连通测试失败，请检查 Webhook 配置")
    finally:
        await pusher.stop()


if __name__ == "__main__":
    asyncio.run(test())
