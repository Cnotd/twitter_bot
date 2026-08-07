"""
Owly X 情报评分引擎
====================
按文档规范实现：账号匹配 → 重要性分级(S/A/B) → 来源优先级 → 信息过滤 → 综合评分
"""

import re
import math
import asyncio
from typing import Dict, List, Tuple, Optional, Set
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from loguru import logger

from tw_config import (
    Importance, SourceLevel, GradingRule, IMPORTANCE_RULES,
    DEDUPED_ACCOUNTS, MonitoredAccount, ACCOUNTS_BY_TIER,
    EXCLUSION_KEYWORDS, TRIGGER_KEYWORDS,
    tw_config,
)
from twitter_client import Tweet


@dataclass
class AccountMatch:
    """账号匹配结果"""
    account: MonitoredAccount
    match_confidence: float = 1.0  # 完全匹配=1.0


@dataclass
class ScoredTweet:
    """评分+分类后的情报条目"""
    tweet: Tweet
    # 基础信息
    source_level: SourceLevel = SourceLevel.TERTIARY
    matched_accounts: List[AccountMatch] = field(default_factory=list)
    matched_tier: str = ""  # P1-P5 / ECO
    # 评分
    importance: Importance = Importance.B
    importance_score: float = 0.0   # 重要性原始分
    relevance_score: float = 0.0    # 相关性分
    freshness_score: float = 0.0    # 时效性分
    total_score: float = 0.0        # 综合分
    # 优先级
    priority: str = "normal"  # urgent / high / normal / low / skip
    # 过滤
    is_filtered: bool = False
    filter_reason: str = ""
    # 核验
    needs_verification: bool = False
    verification_note: str = ""
    # 摘要
    summary: str = ""
    one_liner: str = ""  # 一句话结论用

    @property
    def tier_order(self) -> int:
        """层级排序权重（P1最高 = 0）"""
        order = {"P1": 0, "P2": 1, "P3": 2, "P4": 3, "P5": 4, "ECO": 5}
        return order.get(self.matched_tier, 99)

    def to_dict(self) -> Dict:
        return {
            "tweet_id": self.tweet.tweet_id,
            "author": f"@{self.tweet.author_username}",
            "text_preview": self.tweet.text[:120],
            "matched_tier": self.matched_tier,
            "source_level": self.source_level.name,
            "importance": self.importance.value,
            "total_score": round(self.total_score, 2),
            "priority": self.priority,
            "is_filtered": self.is_filtered,
            "filter_reason": self.filter_reason,
        }

    def to_intel_entry(self) -> Dict:
        """转换为情报输出条目"""
        return {
            "importance": f"{self.importance.cn_label}",
            "title": self.tweet.text[:100] if not self.one_liner else self.one_liner,
            "facts": self._extract_facts(),
            "why_matters": self._why_matters(),
            "accounts_involved": [m.account.username for m in self.matched_accounts],
            "source_url": f"https://twitter.com/{self.tweet.author_username}/status/{self.tweet.tweet_id}",
            "source_level": f"{'一级' if self.source_level == SourceLevel.PRIMARY else '二级' if self.source_level == SourceLevel.SECONDARY else '三级'}来源",
        }

    def _extract_facts(self) -> str:
        """从推文中提取已确认事实"""
        text = self.tweet.text
        facts = []
        if self.tweet.author_verified:
            facts.append(f"发布者已认证")
        if self.tweet.has_link:
            facts.append("包含外部链接")
        if self.tweet.created_at:
            facts.append(f"发布时间: {self.tweet.created_at.strftime('%Y-%m-%d %H:%M UTC')}")
        return "；".join(facts) if facts else "待核验"

    def _why_matters(self) -> str:
        """为什么重要"""
        reasons = []
        tier = self.matched_tier
        if tier == "P1":
            reasons.append("官方/核心团队直接发布")
        elif tier == "ECO":
            reasons.append(f"Hyperliquid 生态项目动态")
        if self.importance == Importance.S:
            reasons.append("可能影响协议/安全/市场结构")
        elif self.importance == Importance.A:
            reasons.append("产品/流动性/生态层面重要进展")
        return "；".join(reasons) if reasons else "需进一步评估"


class FilterEngine:
    """信息过滤器"""

    def __init__(self):
        self._init_patterns()

    def _init_patterns(self):
        self.moon_pattern = re.compile(
            r'wen\s+moon|wen\s+lambo|to\s+the\s+moon|'
            r'pump\s+it|send\s+it|ngmi|wagmi',
            re.IGNORECASE
        )
        self.giveaway_pattern = re.compile(
            r'giveaway|airdrop\s*claim|free\s*mint|drop\s*your\s*wallet|'
            r'drop\s*your\s*address|claim\s*now',
            re.IGNORECASE
        )
        self.time_pattern = re.compile(r'\d{1,2}:\d{2}\s*(AM|PM|UTC)?')

    def should_filter(self, tweet: Tweet) -> Tuple[bool, str]:
        """
        检查是否应过滤
        返回: (是否过滤, 过滤原因)
        """
        text = tweet.text.lower() if tweet.text else ""

        # 1. 纯情绪喊单
        if self.moon_pattern.search(text):
            return True, "纯情绪喊单"

        # 2. 钓鱼/垃圾
        if self.giveaway_pattern.search(text):
            return True, "疑似钓鱼/垃圾信息"

        # 3. 无新增信息的转推
        if tweet.is_reply and not tweet.is_quote:
            if len(tweet.text.strip()) < 50:
                return True, "无新增信息的回复"

        # 4. 只有泛推广、没有产品或数据增量
        if len(text) < 30 and not tweet.has_link:
            return True, "内容过于简短，无实质信息"

        # 5. 排除关键词命中
        for kw in EXCLUSION_KEYWORDS:
            if kw.lower() in text:
                return True, f"命中排除关键词: {kw}"

        return False, ""


class ScoringEngine:
    """
    情报评分引擎
    评分流程：匹配账号池 → 过滤 → 重要性分级 → 来源优先级 → 综合评分
    """

    def __init__(self):
        self.filter_engine = FilterEngine()
        self._build_lookup_index()

    def _build_lookup_index(self):
        """构建账号快速查找索引"""
        self.account_map: Dict[str, MonitoredAccount] = {}
        for acc in DEDUPED_ACCOUNTS:
            self.account_map[acc.username.lower()] = acc

    # ==================== 主评分入口 ====================
    def score_tweet(self, tweet: Tweet) -> ScoredTweet:
        """
        对单条推文进行完整评分与分级
        """
        scored = ScoredTweet(tweet=tweet)

        # Step 0: 信息过滤
        is_filtered, reason = self.filter_engine.should_filter(tweet)
        if is_filtered:
            scored.is_filtered = True
            scored.filter_reason = reason
            scored.priority = "skip"
            scored.total_score = 0
            return scored

        # Step 1: 账号匹配
        scored.matched_accounts = self._match_accounts(tweet)
        if scored.matched_accounts:
            primary_match = scored.matched_accounts[0].account
            scored.matched_tier = primary_match.tier
            scored.source_level = primary_match.source_level
        else:
            scored.matched_tier = ""
            scored.source_level = SourceLevel.TERTIARY

        # Step 2: 计算子分数
        scored.relevance_score = self._score_relevance(tweet)
        scored.importance_score = self._score_importance(tweet)
        scored.freshness_score = self._score_freshness(tweet)

        # Step 3: 确定重要性等级
        scored.importance = self._determine_importance(tweet, scored)

        # Step 4: 综合评分
        # 权重：重要性50% + 相关性30% + 时效性10% + 来源加成10%
        source_bonus = self._source_bonus(scored.source_level)
        scored.total_score = (
            scored.importance_score * 0.50 +
            scored.relevance_score * 0.30 +
            scored.freshness_score * 0.10 +
            source_bonus * 0.10
        )
        scored.total_score = max(0, min(100, scored.total_score))

        # Step 5: 优先级
        scored.priority = self._determine_priority(scored)

        # Step 6: 核验标记
        scored.needs_verification, scored.verification_note = self._check_verification(tweet, scored)

        # Step 7: 摘要
        scored.one_liner = self._generate_one_liner(tweet, scored)
        scored.summary = self._generate_summary(scored)

        return scored

    def batch_score(self, tweets: List[Tweet]) -> List[ScoredTweet]:
        """批量评分，按重要性+分数排序"""
        scored_list = [self.score_tweet(t) for t in tweets]
        # 过滤被排除的
        scored_list = [s for s in scored_list if not s.is_filtered]
        # S→A→B 排序，同级别按分数降序
        imp_order = {Importance.S: 0, Importance.A: 1, Importance.B: 2}
        scored_list.sort(key=lambda s: (
            imp_order.get(s.importance, 9),
            -s.total_score
        ))
        return scored_list

    def should_push(self, scored: ScoredTweet) -> bool:
        """判定是否应推送"""
        if scored.is_filtered:
            return False
        if scored.importance in (Importance.S, Importance.A):
            return True  # S/A 级始终推送
        return scored.total_score >= tw_config.lark.push_threshold

    # ==================== 账号匹配 ====================
    def _match_accounts(self, tweet: Tweet) -> List[AccountMatch]:
        """匹配被监控账号池"""
        matches = []
        username = tweet.author_username.lower()

        if username in self.account_map:
            matches.append(AccountMatch(
                account=self.account_map[username],
                match_confidence=1.0,
            ))

        # 检查推文中 @ 提及的其他监控账号
        mention_pattern = re.compile(r'@(\w+)')
        mentioned = mention_pattern.findall(tweet.text)
        for m in mentioned:
            m_lower = m.lower()
            if m_lower in self.account_map and m_lower != username:
                acc = self.account_map[m_lower]
                if not any(a.account.username.lower() == m_lower for a in matches):
                    matches.append(AccountMatch(account=acc, match_confidence=0.5))

        return matches

    # ==================== 各维度评分 ====================
    def _score_relevance(self, tweet: Tweet) -> float:
        """相关性评分：与 Hyperliquid 生态的关联度"""
        text = tweet.text.lower()
        score = 0.0

        # 核心关键词加权
        core_terms = {
            "hyperliquid": 30, "hype": 20, "$hype": 25,
            "hypercore": 25, "hyperevm": 25, "hl": 10,
            "hip": 15, "hip-1": 20, "hip-2": 20, "hip-3": 20,
            "hypefi": 15, "hyperdefi": 15,
        }
        for term, weight in core_terms.items():
            if term in text:
                score += weight

        # 生态关键词
        eco_terms = [
            "staking", "bridge", "perp", "derivative", "orderbook",
            "auction", "vault", "validator", "spot", "hlp",
            "trading", "liquidity", "funding", "open interest",
        ]
        score += sum(5 for t in eco_terms if t in text)

        # 账号层级加成
        if tweet.author_username.lower() in ACCOUNTS_BY_TIER.get("P1", []):
            score += 30
        elif tweet.author_username.lower() in ACCOUNTS_BY_TIER.get("P2", []):
            score += 20
        elif tweet.author_username.lower() in ACCOUNTS_BY_TIER.get("ECO", []):
            score += 25

        return min(100, max(0, score))

    def _score_importance(self, tweet: Tweet) -> float:
        """重要性原始评分"""
        text = tweet.text.lower()
        score = 20.0  # 基础分

        # S级关键词
        s_keywords = [
            "protocol change", "security incident", "exploit", "hack",
            "vulnerability", "regulatory", "sec ", "cftc",
            "emergency", "critical", "halted", "paused",
            "major upgrade", "hard fork", "mainnet launch",
            "binance listing", "coinbase listing",
        ]
        for kw in s_keywords:
            if kw in text:
                score += 35

        # A级关键词
        a_keywords = [
            "product launch", "new feature", "integration",
            "partner", "partnership", "fundraising", "funding",
            "tvl", "volume", "revenue",
            "sdk", "api", "developer", "incentive", "reward",
            "token launch", "tge", "ido",
        ]
        for kw in a_keywords:
            if kw in text:
                score += 20

        # B级关键词
        b_keywords = [
            "analysis", "strategy", "alpha", "insight",
            "thread", "deep dive", "chart",
        ]
        for kw in b_keywords:
            if kw in text:
                score += 10

        # 互动数据加成（传播热度暗示重要性）
        engagement = math.log1p(tweet.likes + tweet.retweets * 2 + tweet.replies)
        score += min(15, engagement * 3)

        return min(100, max(0, score))

    def _score_freshness(self, tweet: Tweet) -> float:
        """时效性评分"""
        if not tweet.created_at:
            return 40.0
        
        now = datetime.now(timezone.utc)
        age_minutes = (now - tweet.created_at).total_seconds() / 60
        
        if age_minutes <= 10:
            return 100.0
        elif age_minutes <= 30:
            return 90.0
        elif age_minutes <= 60:
            return 75.0
        elif age_minutes <= 120:
            return 60.0
        elif age_minutes <= 360:
            return 40.0
        elif age_minutes <= 1440:  # 24h
            return 20.0
        else:
            return 5.0

    def _source_bonus(self, level: SourceLevel) -> float:
        """来源优先级加成"""
        return {SourceLevel.PRIMARY: 100, SourceLevel.SECONDARY: 60, SourceLevel.TERTIARY: 20}[level]

    # ==================== 重要性分级 ====================
    def _determine_importance(self, tweet: Tweet, scored: ScoredTweet) -> Importance:
        """判定 S/A/B 重要性等级"""
        text = tweet.text.lower()
        tier = scored.matched_tier
        
        # S 级判定
        s_rule = IMPORTANCE_RULES[0].conditions
        s_kws = [kw.lower() for kw in s_rule["keywords"]]
        s_hits = sum(1 for kw in s_kws if kw in text)
        
        s_urgent = s_rule.get("urgent_words", [])
        s_urgent_hits = any(w.lower() in text for w in s_urgent)
        
        # P1/P2 来源 + S级关键词命中 ≥ 2 → S级
        if tier in ("P1", "P2") and (s_hits >= 2 or s_urgent_hits):
            return Importance.S
        
        # P2 以下来源 + 强S级信号 → S级
        if s_hits >= 3 or (s_hits >= 1 and s_urgent_hits):
            return Importance.S
        
        # A 级判定
        a_rule = IMPORTANCE_RULES[1].conditions
        a_kws = [kw.lower() for kw in a_rule["keywords"]]
        a_hits = sum(1 for kw in a_kws if kw in text)
        
        if a_hits >= 1:
            return Importance.A
        if scored.relevance_score >= 50 and tier in ("P1", "P2", "ECO"):
            return Importance.A
        
        # B 级判定
        b_rule = IMPORTANCE_RULES[2].conditions
        b_kws = [kw.lower() for kw in b_rule["keywords"]]
        b_hits = sum(1 for kw in b_kws if kw in text)
        
        if b_hits >= 1 or scored.relevance_score >= 30:
            return Importance.B
        
        return Importance.B

    # ==================== 核验标记 ====================
    def _check_verification(self, tweet: Tweet, scored: ScoredTweet) -> Tuple[bool, str]:
        """检查是否需要额外核验"""
        reasons = []

        # 非官方来源的 S/A 级事件必须核验
        if scored.importance in (Importance.S, Importance.A):
            if scored.source_level == SourceLevel.TERTIARY:
                reasons.append("三级来源的 S/A 级事件需找官方原帖核验")
            elif scored.source_level == SourceLevel.SECONDARY:
                reasons.append("建议找一级来源交叉验证")

        # 无原帖链接的内容
        if not tweet.has_link:
            if scored.importance == Importance.S:
                reasons.append("S级事件缺少外部链接/原始来源")

        # 旧内容
        if scored.freshness_score < 30:
            reasons.append(f"时效性较低({scored.freshness_score:.0f})，请确认是最新信息")

        needs = len(reasons) > 0
        return needs, "；".join(reasons) if reasons else ""

    # ==================== 优先级 ====================
    def _determine_priority(self, scored: ScoredTweet) -> str:
        """确定推送优先级"""
        if scored.importance == Importance.S:
            return "urgent"
        elif scored.importance == Importance.A:
            return "high"
        elif scored.total_score >= 50:
            return "normal"
        elif scored.total_score >= 30:
            return "low"
        else:
            return "skip"

    # ==================== 摘要生成 ====================
    def _generate_one_liner(self, tweet: Tweet, scored: ScoredTweet) -> str:
        """生成一句话结论"""
        author = f"@{tweet.author_username}"
        tier = scored.matched_tier

        if tier == "P1":
            if scored.importance == Importance.S:
                return f"[{scored.importance.value}] Hyperliquid 官方/核心团队发布重要更新"
            return f"[{scored.importance.value}] Hyperliquid 官方动态"
        elif tier == "ECO":
            return f"[{scored.importance.value}] 生态项目 {author} 更新"
        elif scored.importance == Importance.S:
            return f"[S] 疑似重要安全/协议事件，来自 {author}，需核验"
        elif scored.importance == Importance.A:
            return f"[A] 重要动态，来自 {author}"
        else:
            return f"[B] {author}: {tweet.text[:60]}..."

    def _generate_summary(self, scored: ScoredTweet) -> str:
        parts = []
        if scored.matched_tier:
            parts.append(f"层级:{scored.matched_tier}")
        parts.append(f"重要性:{scored.importance.value}")
        parts.append(f"来源:{scored.source_level.name}")
        if scored.needs_verification:
            parts.append("⚠需核验")
        return " | ".join(parts)


# ==================== 情报报告构建器 ====================
class IntelReportBuilder:
    """
    按文档"内部情报输出结构"构建报告
    """

    def __init__(self, scored_tweets: List[ScoredTweet], scan_start: datetime, scan_end: datetime):
        self.scored = scored_tweets
        self.scan_start = scan_start
        self.scan_end = scan_end
        self._categorize()

    def _categorize(self):
        """按类型分类"""
        self.s_items = [s for s in self.scored if s.importance == Importance.S]
        self.a_items = [s for s in self.scored if s.importance == Importance.A]
        self.b_items = [s for s in self.scored if s.importance == Importance.B]
        self.eco_items = [s for s in self.scored if s.matched_tier == "ECO"]
        self.official_items = [s for s in self.scored if s.matched_tier in ("P1", "P2")]

    def build_conclusion(self) -> str:
        """一句话结论"""
        if self.s_items:
            item = self.s_items[0]
            return item.one_liner

        if self.a_items:
            item = self.a_items[0]
            return item.one_liner

        if self.scored:
            return f"监测到 {len(self.scored)} 条 Hyperliquid 生态相关动态，无 S/A 级事件"

        return "本轮未监测到 Hyperliquid 生态相关更新"

    def build_key_events(self) -> List[Dict]:
        """今日要闻"""
        events = []

        # S/A 级优先
        for item in self.s_items + self.a_items:
            events.append(item.to_intel_entry())

        # 值得关注的 B 级
        for item in self.b_items[:5]:
            if item.total_score >= 40:
                events.append(item.to_intel_entry())

        return events

    def build_eco_news(self) -> List[Dict]:
        """生态项目新闻（从 builder 视角）"""
        news = []
        for item in self.eco_items:
            if item.importance in (Importance.S, Importance.A):
                news.append(item.to_intel_entry())
        return news

    def build_data_signals(self) -> List[Dict]:
        """数据与市场信号（有可靠来源的指标）"""
        signals = []
        data_keywords = [
            "tvl", "volume", "revenue", "apy", "apr",
            "open interest", "funding rate", "liquidation",
            "market cap", "$", "price",
        ]
        for item in self.b_items:
            text = item.tweet.text.lower()
            if any(kw in text for kw in data_keywords):
                if item.tweet.has_link:  # 有链接 → 可核验
                    signals.append(item.to_intel_entry())
        return signals[:5]

    def build_watchlist(self) -> List[Dict]:
        """值得继续跟踪"""
        watchlist = []
        # 需要核验但尚未完整确认的
        for item in self.scored:
            if item.needs_verification and item.importance in (Importance.S, Importance.A):
                watchlist.append({
                    "item": item.one_liner,
                    "trigger": f"等待一级来源确认 | 原帖: {item.tweet.text[:60]}",
                    "source": f"https://twitter.com/{item.tweet.author_username}/status/{item.tweet.tweet_id}",
                })
        return watchlist[:3]

    def build_full_report(self) -> Dict:
        """构建完整情报报告"""
        return {
            "report_time": datetime.now(timezone.utc).isoformat(),
            "scan_window": {
                "start": self.scan_start.isoformat(),
                "end": self.scan_end.isoformat(),
            },
            "conclusion": self.build_conclusion(),
            "summary": {
                "total_scanned": len(self.scored),
                "s_count": len(self.s_items),
                "a_count": len(self.a_items),
                "b_count": len(self.b_items),
                "eco_count": len(self.eco_items),
            },
            "key_events": self.build_key_events(),
            "eco_news": self.build_eco_news(),
            "data_signals": self.build_data_signals(),
            "watchlist": self.build_watchlist(),
            "data_status": self._build_data_status(),
        }

    def _build_data_status(self) -> Dict:
        """数据获取状态"""
        return {
            "x_access": "正常" if len(self.scored) > 0 else "本轮未获取到新数据",
            "time_verifiable": True,
            "accounts_checked": len(set(s.tweet.author_username for s in self.scored)),
            "accounts_limited": [],  # API限制时填充
            "fallback_used": False,
            "data_sufficient": len(self.scored) > 0,
        }


# ==================== 测试 ====================
def test_scoring_engine():
    """测试评分引擎"""
    engine = ScoringEngine()

    # 模拟一条 Hyperliquid 官方推文
    tweet = Tweet(
        tweet_id="test_s1",
        author_id="1",
        author_username="HyperliquidX",
        author_name="Hyperliquid",
        author_verified=True,
        author_followers=500000,
        text="Announcing HyperCore mainnet launch! The biggest upgrade "
             "to the Hyperliquid ecosystem. HIP-3 compliant trading "
             "frontends now supported. #Hyperliquid #HyperCore",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=15),
        likes=8000,
        retweets=3000,
        replies=500,
        has_image=True,
        has_link=True,
        lang="en",
    )

    scored = engine.score_tweet(tweet)
    print(f"\n{'='*60}")
    print(f"测试: @{tweet.author_username}")
    print(f"{'='*60}")
    print(f"匹配层级: {scored.matched_tier}")
    print(f"来源级别: {scored.source_level.name}")
    print(f"重要性: {scored.importance.value} ({scored.importance.description})")
    print(f"综合评分: {scored.total_score:.1f}")
    print(f"相关性: {scored.relevance_score:.1f} | 重要性: {scored.importance_score:.1f} | 时效性: {scored.freshness_score:.1f}")
    print(f"优先级: {scored.priority}")
    print(f"一句话: {scored.one_liner}")
    print(f"需核验: {scored.needs_verification} ({scored.verification_note})")
    print(f"是否推送: {engine.should_push(scored)}")

    # 模拟一条 B 级分析推文
    tweet2 = Tweet(
        tweet_id="test_b1",
        author_id="2",
        author_username="HYPEconomist",
        author_name="HYPEconomist",
        author_verified=False,
        author_followers=5000,
        text="Deep dive thread: Hyperliquid TVL analysis Q2 2026. "
             "Key metrics show 45% growth in bridged assets.",
        created_at=datetime.now(timezone.utc) - timedelta(hours=3),
        likes=200,
        retweets=80,
        replies=30,
        has_link=True,
        lang="en",
    )
    scored2 = engine.score_tweet(tweet2)
    print(f"\n{'='*60}")
    print(f"测试: @{tweet2.author_username}")
    print(f"匹配层级: {scored2.matched_tier}")
    print(f"重要性: {scored2.importance.value}")
    print(f"综合评分: {scored2.total_score:.1f}")
    print(f"优先级: {scored2.priority}")
    print(f"是否推送: {engine.should_push(scored2)}")

    # 测试过滤
    tweet3 = Tweet(
        tweet_id="test_filter1",
        author_id="3",
        author_username="random_user",
        author_name="Random",
        text="Wen moon? Wen lambo? Pump it! #crypto",
        created_at=datetime.now(timezone.utc),
        lang="en",
    )
    scored3 = engine.score_tweet(tweet3)
    print(f"\n过滤测试: {scored3.filter_reason}, 已过滤={scored3.is_filtered}")

    # 测试报告构建
    print(f"\n{'='*60}")
    print("情报报告测试")
    print(f"{'='*60}")
    now = datetime.now(timezone.utc)
    report = IntelReportBuilder(
        [scored, scored2],
        scan_start=now - timedelta(hours=24),
        scan_end=now,
    )
    full = report.build_full_report()
    print(f"结论: {full['conclusion']}")
    print(f"S级:{full['summary']['s_count']} A级:{full['summary']['a_count']} B级:{full['summary']['b_count']}")


if __name__ == "__main__":
    test_scoring_engine()
