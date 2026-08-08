"""
Owly X 情报监控与选题来源 — 配置文件
========================================
按文档规范：账号池、重要性分级、来源优先级、过滤规则、输出结构
"""

import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set
from enum import Enum


# ==================== API 凭据 ====================
@dataclass
class TwitterConfig:
    bearer_token: str = os.getenv("TWITTER_BEARER_TOKEN", "")
    api_key: str = os.getenv("TWITTER_API_KEY", "")
    api_secret: str = os.getenv("TWITTER_API_SECRET", "")
    access_token: str = os.getenv("TWITTER_ACCESS_TOKEN", "")
    access_secret: str = os.getenv("TWITTER_ACCESS_SECRET", "")
    max_requests_per_15min: int = 300
    request_delay_sec: float = 0.5


@dataclass
class LarkConfig:
    webhook_url: str = os.getenv("LARK_WEBHOOK_URL", "https://open.larksuite.com/open-apis/bot/v2/hook/f4db43ee-38db-4753-b4f0-b5d100471e8e")
    webhook_secret: str = os.getenv("LARK_WEBHOOK_SECRET", "")
    high_priority_webhook: str = os.getenv("LARK_HIGH_PRIORITY_WEBHOOK", "")
    push_threshold: float = 30.0
    urgent_threshold: float = 70.0
    card_title: str = "🦉 Owly X 情报"


# ==================== 重要性分级 ====================
class Importance(Enum):
    """事件重要性等级"""
    S = "S"  # 协议/安全/重大政策/市场结构变化
    A = "A"  # 重要产品/HIP/上币/关键数据/流动性/生态进展
    B = "B"  # 有价值的分析/策略观察/社区信号

    @property
    def cn_label(self) -> str:
        labels = {"S": "🔴 S 级", "A": "🟠 A 级", "B": "🟡 B 级"}
        return labels[self.value]

    @property
    def description(self) -> str:
        descs = {
            "S": "协议/安全/重大政策/市场结构变化",
            "A": "重要产品/HIP/上币/关键数据/流动性/生态进展",
            "B": "有价值的分析/策略观察/社区信号",
        }
        return descs[self.value]


class SourceLevel(Enum):
    """来源优先级"""
    PRIMARY = 1    # 原始与权威来源
    SECONDARY = 2  # 可信数据与专业分析
    TERTIARY = 3   # 社区观点和线索


# ==================== 核心账号池 ====================
@dataclass
class MonitoredAccount:
    """被监控的 X 账号"""
    username: str           # X 用户名
    name: str = ""          # 显示名
    tier: str = "P5"        # P1-P5
    category: str = ""      # 分类标签
    source_level: SourceLevel = SourceLevel.PRIMARY
    is_official: bool = False
    is_project: bool = False
    notes: str = ""


# ==================== P1 官方与核心团队 ====================
P1_ACCOUNTS: List[MonitoredAccount] = [
    MonitoredAccount("HyperliquidX", "Hyperliquid", "P1",
                     "官方", SourceLevel.PRIMARY, is_official=True),
    MonitoredAccount("chameleon_jeff", "Jeff (Hyperliquid)", "P1",
                     "核心团队", SourceLevel.PRIMARY, is_official=True),
    MonitoredAccount("xulian_hl", "Xulian (Hyperliquid)", "P1",
                     "核心团队", SourceLevel.PRIMARY, is_official=True),
]

# ==================== P2 新闻、数据与聚合 ====================
P2_ACCOUNTS: List[MonitoredAccount] = [
    MonitoredAccount("HYPERDailyTK", "HYPER Daily", "P2",
                     "新闻聚合", SourceLevel.SECONDARY),
    MonitoredAccount("HyperliquidNews", "Hyperliquid News", "P2",
                     "新闻", SourceLevel.SECONDARY),
    MonitoredAccount("Hyperliquid_Hub", "Hyperliquid Hub", "P2",
                     "聚合", SourceLevel.SECONDARY),
    MonitoredAccount("HypurrScan", "HypurrScan", "P2",
                     "数据", SourceLevel.SECONDARY),
]

# ==================== P3 深度分析、基本面与宏观 ====================
P3_ACCOUNTS: List[MonitoredAccount] = [
    MonitoredAccount("HYPEconomist", "HYPEconomist", "P3",
                     "深度分析", SourceLevel.SECONDARY),
    MonitoredAccount("Henrik_on_HL", "Henrik on HL", "P3",
                     "基本面分析", SourceLevel.SECONDARY),
    MonitoredAccount("ThinkingUSD", "ThinkingUSD", "P3",
                     "宏观分析", SourceLevel.SECONDARY),
    MonitoredAccount("louisdives", "Louis Dives", "P3",
                     "深度分析", SourceLevel.SECONDARY),
]

# ==================== P4 社区OG、建设者与交易员 ====================
P4_ACCOUNTS: List[MonitoredAccount] = [
    MonitoredAccount("hypurr_co", "Hypurr", "P4", "社区OG/生态", SourceLevel.TERTIARY),
    MonitoredAccount("vividhl", "Vivid", "P4", "建设者", SourceLevel.TERTIARY),
    MonitoredAccount("GuthixHL", "Guthix", "P4", "建设者", SourceLevel.TERTIARY),
    MonitoredAccount("degennQuant", "Degen Quant", "P4", "交易员", SourceLevel.TERTIARY),
    MonitoredAccount("BOBBYBIGYIELD", "Bobby Big Yield", "P4", "交易员", SourceLevel.TERTIARY),
    MonitoredAccount("0xNessus", "0xNessus", "P4", "建设者", SourceLevel.TERTIARY),
    MonitoredAccount("Sakrexer", "Sakrexer", "P4", "建设者", SourceLevel.TERTIARY),
    MonitoredAccount("reisnertobias", "Tobias Reisner", "P4", "建设者", SourceLevel.TERTIARY),
    MonitoredAccount("NMTD8", "NMTD", "P4", "建设者", SourceLevel.TERTIARY),
    MonitoredAccount("VikingoDigital_", "Vikingo Digital", "P4", "交易员", SourceLevel.TERTIARY),
    MonitoredAccount("theghost_alb", "The Ghost", "P4", "建设者", SourceLevel.TERTIARY),
]

# ==================== P5 额外高价值账号 ====================
P5_ACCOUNTS: List[MonitoredAccount] = [
    MonitoredAccount("blknoiz06", "blknoiz06", "P5", "高价值", SourceLevel.TERTIARY),
]

# ==================== 生态项目观察池 ====================
ECOSYSTEM_ACCOUNTS: List[MonitoredAccount] = [
    # 交易前端、HIP-3 与终端
    MonitoredAccount("tradexyz", "tradexyz", "ECO", "交易前端", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("prjx_hl", "prjx", "ECO", "交易前端", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("Dreamcash", "Dreamcash", "ECO", "交易终端", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("InsilicoTrading", "Insilico", "ECO", "交易终端", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("Dexari", "Dexari", "ECO", "交易前端", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("tradeparagon", "Paragon", "ECO", "交易前端", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("pvp_dot_trade", "PVP Trade", "ECO", "交易前端", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("ventuals", "Ventuals", "ECO", "交易终端", SourceLevel.PRIMARY, is_project=True),
    # 借贷、质押、收益与 DeFi
    MonitoredAccount("Kinetiq_xyz", "Kinetiq", "ECO", "收益/DeFi", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("hyperlendx", "Hyperlend", "ECO", "借贷", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("felixprotocol", "Felix Protocol", "ECO", "DeFi", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("ryskfinance", "Rysk Finance", "ECO", "DeFi", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("unitxyz", "Unit", "ECO", "收益", SourceLevel.PRIMARY, is_project=True),
    # 钱包、数据与基础设施
    MonitoredAccount("Rabby_io", "Rabby", "ECO", "钱包", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("phantom", "Phantom", "ECO", "钱包", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("hypurrdash", "HypurrDash", "ECO", "数据", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("hydromancerxyz", "Hydromancer", "ECO", "基础设施", SourceLevel.PRIMARY, is_project=True),
    # 集体、目录与其他高潜力项目
    MonitoredAccount("HyperliquidPC", "Hyperliquid PC", "ECO", "集体/生态", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("nativemarkets", "Native Markets", "ECO", "市场", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("hypersurface_io", "Hypersurface", "ECO", "高潜力", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("Markets_xyz", "Markets XYZ", "ECO", "市场", SourceLevel.PRIMARY, is_project=True),
    # AI 与策略工具
    MonitoredAccount("minara", "Minara", "ECO", "AI工具", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("senpi_ai", "Senpi AI", "ECO", "AI工具", SourceLevel.PRIMARY, is_project=True),
    MonitoredAccount("nansen_ai", "Nansen AI", "ECO", "AI工具", SourceLevel.TERTIARY, is_project=True),
    MonitoredAccount("pear_protocol", "Pear Protocol", "ECO", "策略工具", SourceLevel.PRIMARY, is_project=True),
]

# 汇总所有账号（去重，hypurr_co 和 HypurrScan 可能在多个层级出现）
ALL_ACCOUNTS = P1_ACCOUNTS + P2_ACCOUNTS + P3_ACCOUNTS + P4_ACCOUNTS + P5_ACCOUNTS + ECOSYSTEM_ACCOUNTS
# 去重（按 username）
_seen = set()
DEDUPED_ACCOUNTS: List[MonitoredAccount] = []
for a in ALL_ACCOUNTS:
    if a.username.lower() not in _seen:
        _seen.add(a.username.lower())
        DEDUPED_ACCOUNTS.append(a)

# 按层级分组的账号列表（仅用户名）
ACCOUNTS_BY_TIER: Dict[str, List[str]] = {
    "P1": [a.username for a in P1_ACCOUNTS],
    "P2": [a.username for a in P2_ACCOUNTS],
    "P3": [a.username for a in P3_ACCOUNTS],
    "P4": [a.username for a in P4_ACCOUNTS],
    "P5": [a.username for a in P5_ACCOUNTS],
    "ECO": [a.username for a in ECOSYSTEM_ACCOUNTS],
}


# ==================== 关键词监控 ====================
# 事件触发型关键词（出现时立即核查，不等到固定日报）
TRIGGER_KEYWORDS = [
    # Hyperliquid 核心
    "Hyperliquid", "HyperCore", "HyperEVM", "HYPE", "$HYPE",
    "HIP-", "HIP1", "HIP2", "HIP3",
    # 安全与协议
    "vulnerability", "exploit", "hack", "security", "audit",
    "bridge", "governance", "upgrade", "mainnet",
    # 业务事件
    "listing", "delisting", "airdrop", "tokenomics", "staking",
    "trading fee", "funding rate", "liquidation",
    # 生态
    "HypeFi", "HyperDeFi", "HL ecosystem",
]

# 否定词（命中这些关键词不触发情报）
EXCLUSION_KEYWORDS = [
    "wen moon", "wen lambo", "to the moon",  # 纯喊单
    "gm", "gn",  # 无信息量的问候
    "giveaway", "airdrop claim", "free mint",  # 钓鱼/垃圾
]


# ==================== S/A/B 重要性判定规则 ====================
@dataclass
class GradingRule:
    """重要性判定规则"""
    importance: Importance
    # 判定条件（满足任一即可）
    conditions: Dict = field(default_factory=dict)


IMPORTANCE_RULES: List[GradingRule] = [
    # S 级：协议、安全、重大政策或市场结构变化
    GradingRule(Importance.S, conditions={
        "keywords": [
            "protocol change", "governance change", "tokenomics change",
            "security incident", "exploit", "hack", "vulnerability disclosed",
            "regulatory", "SEC", "CFTC",
            "exchange listing", "binance listing", "coinbase listing",
            "major upgrade", "mainnet launch", "hard fork",
            "bridge attack", "oracle manipulation",
            "HIP-1", "HIP-2", "HIP-3", "new HIP",
        ],
        "source_tier_min": "P1",      # 至少 P1 来源
        "min_accounts_verified": 1,    # 至少 1 个独立来源确认
        "urgent_words": ["critical", "immediate", "emergency", "incident", "halted", "paused"],
    }),
    # A 级：重要产品、HIP、上币、关键数据、流动性或生态进展
    GradingRule(Importance.A, conditions={
        "keywords": [
            "product launch", "new feature", "integration",
            "partner", "partnership", "collaboration",
            "TVL", "volume", "revenue", "fee",
            "liquidity", "APY", "APR",
            "token launch", "IDO", "TGE",
            "fundraising", "funding round",
            "SDK", "API", "developer tool",
            "incentive", "reward", "points program",
            "HIP", "proposal", "voting",
        ],
        "source_tier_min": "P2",
        "min_accounts_verified": 1,
    }),
    # B 级：有价值的分析、策略观察和社区信号
    GradingRule(Importance.B, conditions={
        "keywords": [
            "analysis", "strategy", "alpha", "insight",
            "chart", "technical analysis",
            "on-chain data", "metrics",
            "comparison", "benchmark",
            "thread", "deep dive",
        ],
        "source_tier_min": "P3",
    }),
]


# ==================== 信息过滤配置 ====================
FILTER_CONFIG = {
    # 必须过滤的内容
    "filter_out": [
        "与 Hyperliquid/HyperCore/HyperEVM/HYPE/HIP 或 Owly 用户无直接关系",
        "纯情绪喊单（wen moon/wen lambo/to the moon）",
        "无新增信息的转推",
        "只有泛推广、没有产品或数据增量的帖子",
        "无法确认发布时间的旧内容",
        "无原帖、无地址或无法核实的传闻",
    ],
    # 转推过滤
    "exclude_retweets": True,
    "exclude_quote_retweets_without_commentary": True,
}


# ==================== KOL 提及处理规则 ====================
KOL_HANDLING_RULES = {
    "steps": [
        "将 KOL 内容当作事件线索",
        "找到并核查项目方原帖",
        "项目方原帖作为主要来源，KOL 讨论作为补充",
        "项目不在固定池也可进入当期候选",
        "是否长期加入观察池由负责人复盘后决定",
        "项目在其他链上的普通更新不自动扩展进 Owly 内容",
    ]
}


# ==================== 系统配置 ====================
@dataclass
class SystemConfig:
    log_level: str = "INFO"
    data_dir: str = "./tw_data"
    last_id_file: str = "last_tweet_ids.json"
    log_file: str = "./logs/twitter_lark.log"
    # 时间窗口（滚动扫描用）
    default_scan_window_hours: int = 24


# ==================== Grok API 配置 ====================
@dataclass
class GrokConfig:
    api_key: str = os.getenv("GROK_API_KEY", "")
    base_url: str = os.getenv("GROK_BASE_URL", "https://api.x.ai/v1")
    model: str = os.getenv("GROK_MODEL", "grok-3-beta")
    max_tokens: int = 4096
    temperature: float = 0.3
    timeout_sec: int = 120


# ==================== 日报配置 ====================
@dataclass
class DailyReportConfig:
    """每日日报配置"""
    # 北京时区 (UTC+8)
    bjt_offset_hours: int = 8
    # 窗口：前一日 09:00 → 当日 09:00 (BJT)
    window_start_hour: int = 9
    # Grok 输出最大中文字符（约 2000-3000 字）
    report_max_chars: int = 3200


# ==================== 主配置 ====================
@dataclass
class TWConfig:
    twitter: TwitterConfig = field(default_factory=TwitterConfig)
    lark: LarkConfig = field(default_factory=LarkConfig)
    system: SystemConfig = field(default_factory=SystemConfig)
    grok: GrokConfig = field(default_factory=GrokConfig)
    daily_report: DailyReportConfig = field(default_factory=DailyReportConfig)
    poll_interval_sec: int = 120
    
    def to_dict(self) -> Dict:
        return {
            "twitter_configured": bool(self.twitter.bearer_token),
            "lark_configured": bool(self.lark.webhook_url),
            "grok_configured": bool(self.grok.api_key),
            "accounts_monitored": len(DEDUPED_ACCOUNTS),
            "poll_interval": self.poll_interval_sec,
            "push_threshold": self.lark.push_threshold,
        }


tw_config = TWConfig()
