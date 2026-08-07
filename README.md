# Owly X 情报监控系统

自动监测 Twitter/X 上 Hyperliquid 生态情报，按 S/A/B 三级重要性打分，推送到飞书群。

## 架构

```
48个监控账号 → Twitter API / Nitter 降级 → 7维评分引擎 → S/A/B 分级 → 飞书卡片推送
```

## 快速部署

### 1. 克隆 + 安装

```bash
git clone <repo_url>
cd <project_dir>
pip install -r tw_requirements.txt
```

### 2. 配置

```bash
# 复制环境变量模板（飞书 Webhook 已预设）
cp .env.example .env

# 如需更高抓取频率，填入 Twitter Bearer Token：
# 编辑 .env → TWITTER_BEARER_TOKEN=你的Token
```

> 不填 Twitter Token 会自动降级到 Nitter RSS（免费，无需注册）。

### 3. 启动

```bash
# 守护模式 — 每5分钟自动轮询（服务器推荐）
python tw_main.py --daemon --interval 300

# 单次扫描最近24小时
python tw_main.py --scan --hours 24

# 事件触发 — 仅检查 P1/P2 重要账号
python tw_main.py --trigger

# 空跑测试 — 不推送到飞书
python tw_main.py --no-push
```

### 4. 推荐：systemd 服务

```bash
sudo tee /etc/systemd/system/owly-intel.service << 'EOF'
[Unit]
Description=Owly X Intel Monitor
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/path/to/project
ExecStart=/usr/bin/python3 /path/to/project/tw_main.py --daemon --interval 300
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now owly-intel
```

## 监控覆盖

| 层级 | 数量 | 说明 |
|------|------|------|
| P1 官方 | 3 | @HyperliquidX @chameleon_jeff @xulian_hl |
| P2 数据 | 4 | @HYPERDailyTK @HyperliquidNews @Hyperliquid_Hub @HypurrScan |
| P3 分析 | 4 | @HYPEconomist @Henrik_on_HL @ThinkingUSD @louisdives |
| P4 社区 | 11 | OG、建设者、交易员 |
| P5 高价值 | 1 | @blknoiz06 |
| ECO 生态 | 25 | 交易前端/DeFi/钱包/基础设施/AI工具 |

## 评分体系

**重要性分级**：
- **S** — 协议安全、重大政策、市场结构变化
- **A** — 重要产品、HIP、上币、关键数据、流动性
- **B** — 有价值的分析、策略观察、社区信号

**7维评分**: 关键词(25%) + 情感(15%) + 传播力(20%) + 账号权重(15%) + 时效性(10%) + 媒体(5%) + 内容质量(10%)

## 飞书输出结构

- 📌 一句话结论
- 📰 今日要闻（重要性/标题/事实/原因/账号/来源）
- 🏗 生态项目新闻（Builder视角）
- 📊 数据与市场信号（可靠来源，注明时间点和口径）
- 🔍 值得继续跟踪（写明触发条件）
- ⚙ 数据获取状态（X访问/降级/覆盖范围/是否影响结论）

## 自定义

编辑 `tw_config.py`：
- `ScoringConfig.rules` — 各维度权重和条件
- `FILTER_KEYWORDS` — 新增过滤词
- `TRIGGER_KEYWORDS` — S/A 事件触发词
- `ACCOUNTS_BY_TIER` — 增减监控账号
