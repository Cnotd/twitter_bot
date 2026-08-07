"""
Owly X 情报监控主控程序
=======================
两种使用方式：
  滚动扫描: python tw_main.py --scan        # 过去24h扫描
  事件触发: python tw_main.py --trigger     # 快速检查重要账号
  守护模式: python tw_main.py --daemon      # 持续轮询

输出结构：
  一句话结论 → 今日要闻 → 生态项目新闻 → 数据与市场信号 → 值得继续跟踪 → 数据获取状态
"""

import asyncio
import json
import os
import sys
import signal
import argparse
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Set
from loguru import logger

# 配置日志
logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    colorize=True,
)
logger.add(
    "logs/twitter_lark.log",
    rotation="10 MB",
    retention="7 days",
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function} | {message}",
    encoding="utf-8",
)

from tw_config import (
    tw_config, DEDUPED_ACCOUNTS, ACCOUNTS_BY_TIER,
    MonitoredAccount, TRIGGER_KEYWORDS,
)
from twitter_client import TwitterClient, NitterFallback, Tweet
from scoring_engine import (
    ScoringEngine, IntelReportBuilder, ScoredTweet,
)
from lark_pusher import LarkPusher


class OwlyIntelBot:
    """Owly 情报机器人"""

    def __init__(self):
        self.twitter = TwitterClient()
        self.nitter = NitterFallback()
        self.scorer = ScoringEngine()
        self.pusher = LarkPusher()
        
        self._running = False
        self._last_ids: Dict[str, str] = {}
        self._pushed_ids: Set[str] = set()
        self._stats = {
            "rounds": 0,
            "tweets_fetched": 0,
            "tweets_pushed": 0,
            "errors": 0,
            "start_time": None,
            "last_scan_start": None,
            "last_scan_end": None,
        }
        self._data_status = {
            "x_access": "未检查",
            "time_verifiable": True,
            "accounts_checked": 0,
            "accounts_limited": [],
            "fallback_used": False,
            "data_sufficient": False,
            "notes": [],
        }

    # ==================== 初始化 ====================
    async def initialize(self):
        logger.info("=" * 60)
        logger.info("🦉 Owly X 情报监控系统 启动中...")
        logger.info("=" * 60)
        
        await self.twitter.start()
        await self.nitter.start()
        await self.pusher.start()
        
        self._load_last_ids()
        
        summary = tw_config.to_dict()
        logger.info(f"监控账号: {summary['accounts_monitored']} 个 (P1-P5 + ECO)")
        logger.info(f"轮询间隔: {summary['poll_interval']}秒")
        logger.info(f"推送阈值: {summary['push_threshold']}分")
        
        tiers = {t: len(list) for t, list in ACCOUNTS_BY_TIER.items()}
        logger.info(f"账号分布: {tiers}")
        logger.info(f"Twitter API: {'已配置' if summary['twitter_configured'] else '未配置（Nitter降级）'}")
        logger.info(f"飞书 Webhook: {'已配置' if summary['lark_configured'] else '未配置'}")
        
        self._stats["start_time"] = datetime.now(timezone.utc)

    async def shutdown(self):
        logger.info("正在关闭...")
        self._running = False
        self._save_last_ids()
        await self.twitter.stop()
        await self.nitter.stop()
        await self.pusher.stop()
        self._print_stats()
        logger.info("系统已关闭")

    # ==================== 模式一：滚动扫描 ====================
    async def scan(self, window_hours: int = 24, push: bool = True) -> Dict:
        """
        滚动扫描模式
        扫描最近 N 小时内的情报，按时间窗口输出
        """
        scan_end = datetime.now(timezone.utc)
        scan_start = scan_end - timedelta(hours=window_hours)
        
        self._stats["last_scan_start"] = scan_start
        self._stats["last_scan_end"] = scan_end
        
        logger.info("=" * 60)
        logger.info(f"滚动扫描 | 窗口: {scan_start.strftime('%Y-%m-%d %H:%M')} → {scan_end.strftime('%Y-%m-%d %H:%M')} UTC")
        logger.info("=" * 60)
        
        # 采集
        all_tweets = await self._fetch_all()
        
        # 时间过滤（只保留扫描窗口内的推文）
        filtered_tweets = self._filter_by_time(all_tweets, scan_start, scan_end)
        total_fetched = sum(len(v) for v in filtered_tweets.values())
        logger.info(f"采集: {total_fetched} 条（扫描窗口内）")
        
        # 评分
        all_scored = []
        for username, tweets in filtered_tweets.items():
            for tweet in tweets:
                if tweet.tweet_id in self._pushed_ids:
                    continue
                scored = self.scorer.score_tweet(tweet)
                if not scored.is_filtered:
                    all_scored.append(scored)
        
        all_scored.sort(key=lambda s: (
            0 if s.importance.name == "S" else 1 if s.importance.name == "A" else 2,
            -s.total_score
        ))
        
        # 构建报告
        report_builder = IntelReportBuilder(
            scored_tweets=all_scored,
            scan_start=scan_start,
            scan_end=scan_end,
        )
        report = report_builder.build_full_report()
        
        # 补充数据状态
        report["data_status"] = self._data_status
        
        # 打印结论
        logger.info(f"\n{'='*60}")
        logger.info(f"📌 {report['conclusion']}")
        logger.info(f"{'='*60}")
        
        s_count = report["summary"]["s_count"]
        a_count = report["summary"]["a_count"]
        b_count = report["summary"]["b_count"]
        logger.info(f"S级:{s_count} | A级:{a_count} | B级:{b_count} | 生态:{report['summary']['eco_count']}")
        
        # 推送
        if push and self.pusher:
            pushable = [s for s in all_scored if self.scorer.should_push(s)]
            if pushable:
                push_result = await self.pusher.push_report(report)
                self._stats["tweets_pushed"] += push_result["sent"]
                
                for s in pushable:
                    self._pushed_ids.add(s.tweet.tweet_id)
        
        # 更新 last_id
        self._update_last_ids(filtered_tweets)
        
        self._stats["rounds"] += 1
        
        return report

    # ==================== 模式二：事件触发 ====================
    async def trigger_check(self, push: bool = True) -> Dict:
        """
        事件触发模式：快速检查 P1/P2 和触发关键词
        用于重大更新时立即核查
        """
        logger.info("=" * 60)
        logger.info("🔔 事件触发检查")
        logger.info("=" * 60)
        
        # 只查 P1+P2（最高优先级）
        all_tweets = {}
        for tier in ("P1", "P2"):
            accounts = [a for a in DEDUPED_ACCOUNTS if a.tier == tier]
            for acc in accounts:
                since_id = self._last_ids.get(acc.username.lower())
                tweets = await self.twitter.fetch_by_account(
                    acc, since_id=since_id, max_results=5
                )
                if tweets:
                    all_tweets[acc.username] = tweets
        
        total = sum(len(v) for v in all_tweets.values())
        logger.info(f"快速采集: {total} 条 (P1+P2)")
        
        # 评分
        all_scored = []
        for tweets in all_tweets.values():
            for tweet in tweets:
                if tweet.tweet_id in self._pushed_ids:
                    continue
                scored = self.scorer.score_tweet(tweet)
                if not scored.is_filtered:
                    all_scored.append(scored)
        
        # 只关注 S/A 级
        urgent = [s for s in all_scored if s.importance.name in ("S", "A")]
        
        if not urgent:
            logger.info("无 S/A 级事件")
            return {"conclusion": "无紧急事件", "key_events": [], "summary": {"s_count": 0, "a_count": 0}}
        
        logger.info(f"发现 {len(urgent)} 条重要事件!")
        
        scan_now = datetime.now(timezone.utc)
        report_builder = IntelReportBuilder(urgent, scan_now - timedelta(hours=6), scan_now)
        report = report_builder.build_full_report()
        
        logger.info(f"📌 {report['conclusion']}")
        
        if push and self.pusher:
            push_result = await self.pusher.push_report(report)
            self._stats["tweets_pushed"] += push_result["sent"]
            for s in urgent:
                self._pushed_ids.add(s.tweet.tweet_id)
        
        self._update_last_ids(all_tweets)
        
        return report

    # ==================== 守护模式 ====================
    async def daemon(self, interval: int = None):
        """持续轮询守护模式"""
        interval = interval or tw_config.poll_interval_sec
        self._running = True
        
        logger.info(f"守护模式: 每 {interval} 秒轮询")
        
        while self._running:
            self._stats["rounds"] += 1
            await self.scan(window_hours=24, push=True)
            
            logger.info(f"下一轮: {interval}秒后...")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
    
    # ==================== 数据采集 ====================
    async def _fetch_all(self) -> Dict[str, List[Tweet]]:
        """采集所有监控账号"""
        self._data_status = {
            "x_access": "正常",
            "time_verifiable": True,
            "accounts_checked": 0,
            "accounts_limited": [],
            "fallback_used": False,
            "data_sufficient": False,
            "notes": [],
        }
        
        if tw_config.twitter.bearer_token:
            results = await self.twitter.fetch_all_monitored(
                last_ids=self._last_ids,
                max_per_account=10,
            )
            self._data_status["x_access"] = "正常（API v2）"
        else:
            self._data_status["fallback_used"] = True
            self._data_status["notes"].append("未配置 Twitter API，使用 Nitter RSS 降级")
            self._data_status["x_access"] = "降级（Nitter RSS）"
            
            results = {}
            for acc in DEDUPED_ACCOUNTS:
                since_id = self._last_ids.get(acc.username.lower())
                tweets = await self.nitter.get_user_tweets(
                    acc.username, max_results=5
                )
                if since_id:
                    tweets = [t for t in tweets if t.tweet_id > since_id]
                if tweets:
                    results[acc.username] = tweets
        
        checked = len(results)
        limited = []
        total_accounts = len(DEDUPED_ACCOUNTS)
        
        if checked < total_accounts:
            if not tw_config.twitter.bearer_token:
                limited.append("Nitter 降级仅支持部分 RSS 源")
        
        self._data_status["accounts_checked"] = checked
        self._data_status["accounts_limited"] = limited
        self._data_status["data_sufficient"] = checked > 0
        
        if checked == 0:
            self._data_status["notes"].append("本轮未获取到任何新推文")
            self._data_status["data_sufficient"] = False
        
        return results

    def _filter_by_time(
        self, all_tweets: Dict[str, List[Tweet]],
        start: datetime, end: datetime,
    ) -> Dict[str, List[Tweet]]:
        """按时间窗口过滤推文"""
        filtered = {}
        for username, tweets in all_tweets.items():
            in_window = []
            for t in tweets:
                if t.created_at and start <= t.created_at <= end:
                    in_window.append(t)
            if in_window:
                filtered[username] = in_window
        return filtered

    # ==================== ID 持久化 ====================
    def _load_last_ids(self):
        data_dir = tw_config.system.data_dir
        filepath = os.path.join(data_dir, tw_config.system.last_id_file)
        
        try:
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._last_ids = data.get("last_ids", {})
                    self._pushed_ids = set(data.get("pushed_ids", []))
                logger.info(f"加载历史: {len(self._last_ids)}个源, {len(self._pushed_ids)}条已推送")
        except Exception as e:
            logger.warning(f"加载历史失败: {e}")
            self._last_ids = {}
            self._pushed_ids = set()
        
        os.makedirs(data_dir, exist_ok=True)

    def _save_last_ids(self):
        data_dir = tw_config.system.data_dir
        filepath = os.path.join(data_dir, tw_config.system.last_id_file)
        
        try:
            pushed_list = list(self._pushed_ids)[-10000:]
            data = {
                "last_ids": self._last_ids,
                "pushed_ids": pushed_list,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存记录失败: {e}")

    def _update_last_ids(self, all_tweets: Dict[str, List[Tweet]]):
        for username, tweets in all_tweets.items():
            if tweets:
                latest = max(t.tweet_id for t in tweets if t.tweet_id)
                self._last_ids[username.lower()] = latest

    def _print_stats(self):
        stats = self._stats
        runtime = ""
        if stats["start_time"]:
            delta = datetime.now(timezone.utc) - stats["start_time"]
            h = int(delta.total_seconds() // 3600)
            m = int((delta.total_seconds() % 3600) // 60)
            runtime = f"{h}h{m}m"
        
        logger.info("=" * 40)
        logger.info(f"运行统计 ({runtime}):")
        logger.info(f"  轮次: {stats['rounds']}")
        logger.info(f"  推送: {stats['tweets_pushed']}条")
        logger.info(f"  错误: {stats['errors']}")
        logger.info("=" * 40)


# ==================== CLI ====================
async def main():
    parser = argparse.ArgumentParser(
        description="🦉 Owly X 情报监控系统"
    )
    parser.add_argument("--scan", "-s", action="store_true", default=True,
                        help="滚动扫描模式（默认，最近24h）")
    parser.add_argument("--trigger", "-t", action="store_true",
                        help="事件触发模式（仅检查 P1/P2 重要账号）")
    parser.add_argument("--daemon", "-d", action="store_true",
                        help="持续轮询守护模式")
    parser.add_argument("--hours", type=int, default=24,
                        help="扫描时间窗口（小时），默认24")
    parser.add_argument("--interval", type=int,
                        help="守护模式轮询间隔（秒）")
    parser.add_argument("--no-push", "-n", action="store_true",
                        help="不推送到飞书（仅本地输出）")
    
    args = parser.parse_args()
    
    os.makedirs(tw_config.system.data_dir, exist_ok=True)
    os.makedirs("logs", exist_ok=True)
    
    bot = OwlyIntelBot()
    
    try:
        await bot.initialize()
        
        if args.daemon:
            await bot.daemon(interval=args.interval)
        elif args.trigger:
            await bot.trigger_check(push=not args.no_push)
        else:
            # 默认滚动扫描
            report = await bot.scan(
                window_hours=args.hours,
                push=not args.no_push,
            )
            
            # 输出结构化报告到控制台
            print(f"\n{'='*60}")
            print(f"🦉 Owly 情报报告")
            print(f"{'='*60}")
            print(f"\n📌 一句话结论:\n  {report['conclusion']}")
            
            print(f"\n📰 今日要闻 ({len(report['key_events'])}条):")
            for i, ev in enumerate(report['key_events'][:5], 1):
                print(f"  {ev.get('importance', '')} {ev.get('title', '')[:80]}")
            
            print(f"\n🏗 生态新闻 ({len(report['eco_news'])}条):")
            for ev in report['eco_news'][:3]:
                print(f"  - {ev.get('title', '')[:80]}")
            
            print(f"\n🔍 继续跟踪 ({len(report['watchlist'])}项):")
            for item in report['watchlist']:
                print(f"  - {item.get('item', '')[:60]}")
            
            print(f"\n⚙ 数据状态:")
            ds = report.get('data_status', {})
            for k, v in ds.items():
                print(f"  {k}: {v}")
    
    except KeyboardInterrupt:
        logger.info("用户中断")
    except Exception as e:
        logger.exception(f"致命错误: {e}")
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
