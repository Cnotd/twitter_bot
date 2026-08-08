"""
Owly X 每日情报日报 — 基于 Grok API 生成结构化中文日报
==========================================================
流程: 采集推文 → 评分过滤 → 构建prompt → Grok生成 → 飞书推送
时间窗口: 北京时间前一日 09:00 → 当日 09:00
"""

import json
import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from loguru import logger

from tw_config import (
    tw_config, ACCOUNTS_BY_TIER, DEDUPED_ACCOUNTS,
    Importance,
)
from twitter_client import TwitterClient, Tweet
from scoring_engine import ScoringEngine, ScoredTweet, SourceLevel


# ==================== 北京时间工具函数 ====================
BJT = timezone(timedelta(hours=8))


def _now_bjt() -> datetime:
    return datetime.now(BJT)


def daily_window_bjt() -> Tuple[datetime, datetime]:
    """
    计算北京时间日报窗口: 前一日 09:00 → 当日 09:00
    返回 UTC 时间范围用于推文过滤
    """
    now = _now_bjt()
    today_9am = now.replace(hour=9, minute=0, second=0, microsecond=0)
    start_bjt = today_9am - timedelta(hours=24)
    end_bjt = today_9am
    # 转 UTC
    start_utc = start_bjt.astimezone(timezone.utc)
    end_utc = end_bjt.astimezone(timezone.utc)
    return start_utc, end_utc


def fmt_bjt(dt: datetime) -> str:
    """格式化为北京时间字符串"""
    return dt.astimezone(BJT).strftime("%Y-%m-%d %H:%M BJT")


def fmt_window_label(start_utc: datetime, end_utc: datetime) -> str:
    """生成覆盖窗口说明"""
    s = start_utc.astimezone(BJT)
    e = end_utc.astimezone(BJT)
    return f"北京时间 {s.strftime('%Y-%m-%d %H:%M')} — {e.strftime('%Y-%m-%d %H:%M')}"


# ==================== 推文上下文构建 ====================
def _truncate(text: str, max_len: int = 180) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def build_tweet_context(
    scored_list: List[ScoredTweet],
    start_utc: datetime,
    end_utc: datetime,
    data_status: Dict,
) -> str:
    """
    将评分后的推文列表转为 Grok 可理解的结构化上下文文本
    """
    if not scored_list:
        return "（本轮 24h 窗口内未采集到符合相关性阈值的推文）"

    # 按重要性分组
    s_items = [s for s in scored_list if s.importance == Importance.S]
    a_items = [s for s in scored_list if s.importance == Importance.A]
    b_items = [s for s in scored_list if s.importance == Importance.B]

    lines = [
        f"覆盖窗口: {fmt_window_label(start_utc, end_utc)}",
        f"共采集 {len(scored_list)} 条相关推文: S级 {len(s_items)} | A级 {len(a_items)} | B级 {len(b_items)}",
        f"数据获取: X原帖访问{'正常' if data_status.get('x_access','')=='正常' else '受限'}，已检查{data_status.get('accounts_checked',0)}个账号",
        "=" * 50,
    ]

    # 写入 S 级推送
    if s_items:
        lines.append("\n## S级（协议/安全/重大政策/市场结构）")
        for i, s in enumerate(s_items, 1):
            lines.append(_format_tweet_entry(i, s))

    # A 级
    if a_items:
        lines.append("\n## A级（重要产品/HIP/上币/关键数据/生态进展）")
        for i, s in enumerate(a_items, 1):
            lines.append(_format_tweet_entry(i, s))

    # B 级（仅高分）
    high_b = [s for s in b_items if s.total_score >= 30]
    if high_b:
        lines.append("\n## B级（分析与社区信号 — 仅高分）")
        for i, s in enumerate(high_b, 1):
            lines.append(_format_tweet_entry(i, s))

    return "\n".join(lines)


def _format_tweet_entry(idx: int, s: ScoredTweet) -> str:
    t = s.tweet
    created_str = t.created_at.strftime("%Y-%m-%d %H:%M UTC") if t.created_at else "未知时间"
    url = f"https://twitter.com/{t.author_username}/status/{t.tweet_id}"
    author_label = f"{'[V]' if t.author_verified else ''} @{t.author_username}" + (
        f" ({s.matched_tier})" if s.matched_tier else ""
    )
    return (
        f"{idx}. {author_label} | {created_str} | 评分{s.total_score:.0f}\n"
        f"   内容: {_truncate(t.text)}\n"
        f"   互动: 赞{t.likes} 转{t.retweets} 回{t.replies}\n"
        f"   链接: {url}"
    )


# ==================== Grok 日报 Prompt ====================
DAILY_REPORT_SYSTEM_PROMPT = """你是 Owly 的 Hyperliquid 生态情报分析师。你的任务是根据提供的 X/Twitter 推文数据，生成一份严格结构化的中文「Hyperliquid X 每日情报」日报。

## 输出格式（必须严格按顺序）

# Hyperliquid X 每日情报（YYYY年MM月DD日）

**覆盖窗口**: {window_label}
**报告时间**: {report_time}

---

### 📌 一句话结论
说明当前最重要的变化。没有重大更新时明确写出"本轮未监测到 S 级或 A 级事件"，不制造新闻。

### 📰 今日要闻
每个事件包含：
- **重要性** S/A/B
- **标题**: 简短概括
- **已确认事实**: 从推文内容提取的可核验事实
- **为什么重要**: 对 Hyperliquid 生态的影响
- **涉及账号**: @account
- **来源**: 可点击的 Twitter 链接
- **分级依据**: 如"_三级来源，需交叉核验_"

合并重复事件。仅收录与 Hyperliquid/HyperCore/HyperEVM/HYPE/HIP/生态项目/交易/流动性/链上数据/安全/监管直接相关的内容。
S 级（协议/安全/重大政策/市场结构）→ A 级（产品/HIP/上币/关键数据/流动性/生态进展）→ B 级（分析和社区信号）排序。
明确区分事实、观点、传闻。

### 🏗 生态项目新闻
从 builder 视角，重点收录：
- 产品上线/集成
- 开发者工具/SDK/API
- 激励与费用变化
- TVL/收入/流动性变化
- 治理提案
- 合作/合规/安全/基础设施

过滤：纯喊单、泛推广、无信息增量互动。KOL 引用项目时，标注并优先使用项目方原帖。项目方其他链内容不收录。

### 📊 数据与市场信号
仅收录有可靠来源的指标（TVL、交易量、费率、持仓量、清算等），标注数据时间点和口径。不把旧数据包装为当天变化。

### 🔍 值得继续跟踪
最多 3 项，每项写明：观测内容 + 触发条件。

### 👀 账号动态速览
按 P1→P5→ECO 简述各账号今日发帖态势。

### ⚙ 数据获取状态
1. X 原帖访问
2. 发布时间核验
3. 账号检查情况
4. 降级来源说明
5. 数据不足是否影响结论

---

## 重要规则

1. **不编造**: 绝不编造帖子、时间、数据或链接。所有结论必须基于提供的推文数据。
2. **事实优先**: 显著区分"已确认事实"、"观点"、"传闻"。三级来源引用时标注"需核验"。
3. **实事求是**: 没有更新就说"本轮未监测到重大更新"，不制造新闻。
4. **信息过滤**: 过滤纯喊单、无信息增量的转推、泛推广、钓鱼/垃圾信息。
5. **合并同类**: 多账号讨论同一事件时合并为一个条目。
6. **字数控制**: 全文控制在 2000—3000 个中文字。
7. **可点击来源**: 所有重要结论附 Twitter 原文链接。
8. **北京时区**: 所有时间使用北京时间（BJT, UTC+8）。

## 生态 builder 规则（最高优先级）
- 日报必须包含「生态项目新闻」栏目
- KOL 引用的生态项目动态 → 核查并优先链接项目方原帖
- 不在固定观察池的项目方也可作为本期候选来源
- 项目方其他链上的普通更新不自动扩入
- 过滤：纯喊单、无信息增量的互动

## KOL 提及项目时的处理
1. KOL 内容当作事件线索
2. 找到并核查项目方原帖
3. 项目方原帖作为主要来源，KOL 讨论作为补充
4. 如果项目不在固定观察池，也可进入当期候选
5. 是否长期加入观察池，由负责人复盘后决定"""


# ==================== Grok API 调用 ====================
class GrokClient:
    """Grok API 客户端（xAI，OpenAI 兼容协议）"""

    def __init__(self):
        self.cfg = tw_config.grok
        self.chat_url = f"{self.cfg.base_url.rstrip('/')}/chat/completions"

    async def generate(self, system_prompt: str, user_content: str) -> str:
        """
        调用 Grok 生成日报
        """
        if not self.cfg.api_key:
            raise ValueError("Grok API Key 未配置。请设置环境变量 GROK_API_KEY")

        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.api_key}",
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    self.chat_url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.cfg.timeout_sec),
                ) as resp:
                    body = await resp.json()

                    if resp.status != 200:
                        error_msg = body.get("error", {}).get("message", str(body))
                        logger.error(f"Grok API error [{resp.status}]: {error_msg}")
                        raise RuntimeError(f"Grok API 调用失败: {error_msg}")

                    choices = body.get("choices", [])
                    if not choices:
                        raise RuntimeError("Grok 返回空响应")

                    content = choices[0].get("message", {}).get("content", "")
                    usage = body.get("usage", {})
                    logger.info(
                        f"Grok 生成完成: {len(content)} 字符, "
                        f"tokens: in={usage.get('prompt_tokens','?')} out={usage.get('completion_tokens','?')}"
                    )
                    return content

            except asyncio.TimeoutError:
                raise RuntimeError(f"Grok API 超时 ({self.cfg.timeout_sec}s)")
            except aiohttp.ClientError as e:
                raise RuntimeError(f"Grok API 网络错误: {e}")


# ==================== 日报生成器 ====================
class DailyReportGenerator:
    """
    每日情报日报生成器
    流程: 采集 → 评分 → 构建prompt → Grok生成 → 返回markdown
    """

    def __init__(self):
        self.twitter = TwitterClient()
        self.scorer = ScoringEngine()
        self.grok = GrokClient()

    async def generate_report(self) -> Dict:
        """
        主入口: 生成完整每日情报日报
        """
        start_utc, end_utc = daily_window_bjt()

        logger.info(f"日报窗口: {fmt_window_label(start_utc, end_utc)}")

        # Step 1: 采集推文
        all_tweets, data_status = await self._fetch_tweets(start_utc, end_utc)

        # Step 2: 评分过滤
        scored_list = self.scorer.batch_score(all_tweets)

        # Step 3: 构建 Grok prompt 上下文
        tweet_context = build_tweet_context(scored_list, start_utc, end_utc, data_status)

        # Step 4: 构建 system prompt
        now_bjt = _now_bjt()
        system_prompt = DAILY_REPORT_SYSTEM_PROMPT.format(
            window_label=fmt_window_label(start_utc, end_utc),
            report_time=now_bjt.strftime("%Y-%m-%d %H:%M BJT"),
        )

        # Step 5: 构建 user prompt
        user_prompt = f"""请根据以下 Twitter/X 推文数据，生成 Hyperliquid X 每日情报日报。

## 监控账号池
P1 官方: {', '.join(ACCOUNTS_BY_TIER.get('P1', []))}
P2 数据: {', '.join(ACCOUNTS_BY_TIER.get('P2', []))}
P3 分析: {', '.join(ACCOUNTS_BY_TIER.get('P3', []))}
P4 社区: {', '.join(ACCOUNTS_BY_TIER.get('P4', []))}
P5 高价值: {', '.join(ACCOUNTS_BY_TIER.get('P5', []))}
ECO 生态: {', '.join(ACCOUNTS_BY_TIER.get('ECO', []))}

## 采集到的推文数据

{tweet_context}

---

请严格按照「每日情报日报输出格式」生成日报。今天是 {now_bjt.strftime('%Y年%m月%d日')}。
所有时间使用北京时间（BJT, UTC+8）。
数据不足时如实说明，不编造信息。"""

        # Step 6: 调用 Grok
        logger.info("正在调用 Grok API 生成日报...")
        try:
            report_text = await self.grok.generate(system_prompt, user_prompt)
        except RuntimeError as e:
            # Grok 调用失败时，降级为本地生成的精简报告
            logger.warning(f"Grok 调用失败: {e}，降级为本地报告")
            report_text = self._build_fallback_report(
                scored_list, start_utc, end_utc, data_status
            )

        # Step 7: 组装结果
        return {
            "success": True,
            "window_label": fmt_window_label(start_utc, end_utc),
            "window": {"start_utc": start_utc.isoformat(), "end_utc": end_utc.isoformat()},
            "report_time": now_bjt.isoformat(),
            "report": report_text,
            "stats": {
                "total_tweets": len(all_tweets),
                "scored": len(scored_list),
                "s_count": sum(1 for s in scored_list if s.importance == Importance.S),
                "a_count": sum(1 for s in scored_list if s.importance == Importance.A),
                "b_count": sum(1 for s in scored_list if s.importance == Importance.B),
            },
            "data_status": data_status,
        }

    async def _fetch_tweets(
        self, start_utc: datetime, end_utc: datetime
    ) -> Tuple[List[Tweet], Dict]:
        """
        采集时间窗口内的推文
        """
        data_status = {
            "x_access": "正常",
            "time_verifiable": True,
            "accounts_checked": 0,
            "accounts_failed": [],
            "accounts_limited": [],
            "fallback_used": False,
            "data_sufficient": True,
            "notes": [],
        }

        all_tweets = []
        accounts_to_check = [
            a.username
            for a in DEDUPED_ACCOUNTS
            if a.username not in getattr(tw_config, "_daily_skip", set())
        ]

        data_status["accounts_checked"] = len(accounts_to_check)

        for username in accounts_to_check:
            try:
                tweets = await self.twitter.fetch_user_tweets(username)
                # 过滤时间窗口内的推文
                window_tweets = [
                    t for t in tweets
                    if t.created_at and start_utc <= t.created_at <= end_utc
                ]
                all_tweets.extend(window_tweets)

                if window_tweets:
                    tier = self._lookup_tier(username)
                    logger.debug(f"  @{username} ({tier}): {len(window_tweets)} 条")

            except Exception as e:
                logger.warning(f"  @{username} 获取失败: {e}")
                data_status["accounts_failed"].append(username)

            # 小延迟避免频率限制
            await asyncio.sleep(0.3)

        if not all_tweets:
            data_status["data_sufficient"] = False
            data_status["notes"].append("24h 窗口内所有监控账号均未发推或无法访问")

        logger.info(
            f"采集完成: {len(all_tweets)} 条推文 "
            f"(成功 {data_status['accounts_checked'] - len(data_status['accounts_failed'])}/"
            f"{data_status['accounts_checked']} 个账号)"
        )

        return all_tweets, data_status

    def _lookup_tier(self, username: str) -> str:
        u = username.lower()
        for tier in ["P1", "P2", "P3", "P4", "P5", "ECO"]:
            if u in [a.lower() for a in ACCOUNTS_BY_TIER.get(tier, [])]:
                return tier
        return "?"

    def _build_fallback_report(
        self,
        scored_list: List[ScoredTweet],
        start_utc: datetime,
        end_utc: datetime,
        data_status: Dict,
    ) -> str:
        """
        Grok 不可用时的本地降级报告
        """
        now = _now_bjt()
        lines = [
            f"# Hyperliquid X 每日情报（{now.strftime('%Y年%m月%d日')}）",
            "",
            f"**覆盖窗口**: {fmt_window_label(start_utc, end_utc)}",
            f"**报告时间**: {now.strftime('%Y-%m-%d %H:%M BJT')}",
            f"**⚠ 注意**: Grok API 不可用，以下为本地程序生成的基础报告，建议核实。",
            "",
            "---",
        ]

        # 结论
        s_count = sum(1 for s in scored_list if s.importance == Importance.S)
        a_count = sum(1 for s in scored_list if s.importance == Importance.A)

        if s_count > 0:
            lines.append(f"### 📌 一句话结论")
            lines.append(f"> Hyperliquid 生态发生 S 级事件，共 {s_count} 条，需立即关注。")
        elif a_count > 0:
            lines.append(f"### 📌 一句话结论")
            lines.append(f"共监测到 {a_count} 条 A 级重要动态。")
        elif scored_list:
            lines.append(f"### 📌 一句话结论")
            lines.append(f"本轮未监测到 S 级或 A 级事件，共有 {len(scored_list)} 条生态相关推文。")
        else:
            lines.append(f"### 📌 一句话结论")
            lines.append("本轮未监测到 Hyperliquid 生态相关重大更新。")

        lines.extend(["", "### 📰 今日要闻"])

        # S/A/B 分级展示
        for label, items, prefix in [
            ("S级", [s for s in scored_list if s.importance == Importance.S], "🔴"),
            ("A级", [s for s in scored_list if s.importance == Importance.A], "🟠"),
            ("B级", [s for s in scored_list if s.total_score >= 40], "🔵"),
        ]:
            for s in items:
                t = s.tweet
                url = f"https://twitter.com/{t.author_username}/status/{t.tweet_id}"
                created = t.created_at.strftime("%Y-%m-%d %H:%M UTC") if t.created_at else "?"
                lines.extend([
                    f"**{prefix} [{label}] @{t.author_username}** " +
                    (f"({s.matched_tier}) " if s.matched_tier else ""),
                    f"> {_truncate(t.text, 200)}",
                    f"> 发布日期: {created} | 评分: {s.total_score:.0f}",
                    f"> [查看原帖]({url})",
                    "",
                ])

        # 生态项目
        eco_items = [s for s in scored_list if s.matched_tier == "ECO"]
        if eco_items:
            lines.append("### 🏗 生态项目新闻")
            for s in eco_items:
                t = s.tweet
                url = f"https://twitter.com/{t.author_username}/status/{t.tweet_id}"
                lines.append(f"- **@{t.author_username}**: {_truncate(t.text, 150)} [查看原帖]({url})")
            lines.append("")

        # 数据状态
        lines.extend([
            "### ⚙ 数据获取状态",
            f"1. X 原帖访问: {'正常' if data_status.get('x_access') == '正常' else '受限'}",
            f"2. 发布时间核验: {'可核验' if data_status.get('time_verifiable') else '部分无法核验'}",
            f"3. 账号检查: {data_status.get('accounts_checked', 0)} 个已查, "
            f"{len(data_status.get('accounts_failed', []))} 个失败",
            f"4. 降级来源: Grok API 不可用，报告为本地程序生成",
            f"5. 数据是否影响结论: {'否' if scored_list else '是，数据不足'}",
        ])

        return "\n".join(lines)


# ==================== 测试 ====================
async def test_daily():
    """测试日报生成（仅本地，不调 Grok）"""
    gen = DailyReportGenerator()
    start, end = daily_window_bjt()
    print(f"日报窗口: {fmt_window_label(start, end)}")
    print(f"UTC窗口: {start.isoformat()} → {end.isoformat()}")
    print()

    # 测试降级报告
    from scoring_engine import dummy_tweets_for_test
    tweets = dummy_tweets_for_test()
    engine = ScoringEngine()
    scored = engine.batch_score(tweets)
    fallback = gen._build_fallback_report(scored, start, end, {
        "x_access": "正常",
        "time_verifiable": True,
        "accounts_checked": 48,
        "accounts_failed": [],
        "data_sufficient": True,
    })
    print("=== 降级报告（模拟）===")
    print(fallback[:500] + "...")
    print("\n✅ daily_report.py 模块正常")


if __name__ == "__main__":
    asyncio.run(test_daily())
