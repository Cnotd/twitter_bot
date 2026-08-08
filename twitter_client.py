"""
Twitter / X 数据采集客户端
===========================
使用 Twitter API v2 获取推文，支持 Nitter 降级
适配 Owly X 情报监控账号池
"""

import asyncio
import time
import json
import os
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Set
from datetime import datetime, timezone, timedelta
from loguru import logger

import aiohttp

from tw_config import (
    tw_config, TwitterConfig, DEDUPED_ACCOUNTS, MonitoredAccount,
    ACCOUNTS_BY_TIER,
)


@dataclass
class Tweet:
    """推文数据模型"""
    tweet_id: str
    author_id: str
    author_username: str = ""
    author_name: str = ""
    author_verified: bool = False
    author_followers: int = 0
    
    text: str = ""
    created_at: Optional[datetime] = None
    
    # 互动数据
    likes: int = 0
    retweets: int = 0
    replies: int = 0
    quotes: int = 0
    impressions: int = 0
    
    # 媒体
    has_image: bool = False
    has_video: bool = False
    has_link: bool = False
    has_poll: bool = False
    
    # 引用/回复
    is_reply: bool = False
    is_quote: bool = False
    is_retweet: bool = False
    referenced_tweet_id: str = ""
    
    # 其他
    lang: str = ""
    source: str = ""
    
    # 元数据
    fetch_time: Optional[datetime] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api_response(cls, data: Dict, includes: Dict = None) -> Optional["Tweet"]:
        try:
            if includes is None:
                includes = {}
            
            users_map = {u["id"]: u for u in includes.get("users", [])}
            author = users_map.get(data.get("author_id", ""), {})
            
            media_map = {m["media_key"]: m for m in includes.get("media", [])}
            attachments = data.get("attachments", {})
            media_keys = attachments.get("media_keys", [])
            
            has_image = any(
                media_map.get(k, {}).get("type") == "photo"
                for k in media_keys
            )
            has_video = any(
                media_map.get(k, {}).get("type") in ("video", "animated_gif")
                for k in media_keys
            )
            has_poll = bool(attachments.get("poll_ids"))
            
            entities = data.get("entities", {})
            has_link = bool(entities.get("urls"))
            
            created_at = None
            if data.get("created_at"):
                created_at = datetime.fromisoformat(
                    data["created_at"].replace("Z", "+00:00")
                )
            
            public_metrics = data.get("public_metrics", {})
            
            ref_tweets = data.get("referenced_tweets", [])
            ref_types = [r.get("type") for r in ref_tweets]
            is_retweet = "retweeted" in ref_types
            is_reply = "replied_to" in ref_types
            is_quote = "quoted" in ref_types
            ref_id = ref_tweets[0]["id"] if ref_tweets else ""
            
            return cls(
                tweet_id=data["id"],
                author_id=data.get("author_id", ""),
                author_username=author.get("username", ""),
                author_name=author.get("name", ""),
                author_verified=author.get("verified", False),
                author_followers=author.get("public_metrics", {}).get("followers_count", 0),
                text=data.get("text", ""),
                created_at=created_at,
                likes=public_metrics.get("like_count", 0),
                retweets=public_metrics.get("retweet_count", 0),
                replies=public_metrics.get("reply_count", 0),
                quotes=public_metrics.get("quote_count", 0),
                impressions=public_metrics.get("impression_count", 0),
                has_image=has_image,
                has_video=has_video,
                has_link=has_link,
                has_poll=has_poll,
                is_retweet=is_retweet,
                is_reply=is_reply,
                is_quote=is_quote,
                referenced_tweet_id=ref_id,
                lang=data.get("lang", ""),
                fetch_time=datetime.now(timezone.utc),
                raw=data,
            )
        except Exception as e:
            logger.error(f"Failed to parse tweet: {e}")
            return None


class TwitterClient:
    """Twitter API v2 客户端"""

    BASE_URL = "https://api.twitter.com/2"

    def __init__(self, config: TwitterConfig = None):
        self.cfg = config or tw_config.twitter
        self.session: Optional[aiohttp.ClientSession] = None
        self._last_request_time = 0.0
        self._request_count_15min = 0
        self._window_start = time.time()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    async def start(self):
        if self.session is None:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def stop(self):
        if self.session:
            await self.session.close()
            self.session = None

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.cfg.bearer_token}",
            "User-Agent": "OwlyIntelBot/1.0",
        }

    async def _rate_limit_check(self):
        now = time.time()
        if now - self._window_start > 900:
            self._window_start = now
            self._request_count_15min = 0
        if self._request_count_15min >= self.cfg.max_requests_per_15min:
            wait = 900 - (now - self._window_start)
            logger.warning(f"Rate limit approaching, waiting {wait:.0f}s")
            await asyncio.sleep(wait)
            self._window_start = time.time()
            self._request_count_15min = 0
        elapsed = now - self._last_request_time
        if elapsed < self.cfg.request_delay_sec:
            await asyncio.sleep(self.cfg.request_delay_sec - elapsed)
        self._last_request_time = time.time()
        self._request_count_15min += 1

    async def _get(self, endpoint: str, params: Dict = None) -> Dict:
        await self._rate_limit_check()
        url = f"{self.BASE_URL}{endpoint}"
        headers = self._get_headers()

        for attempt in range(3):
            try:
                async with self.session.get(
                    url, headers=headers, params=params
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 429:
                        retry_after = int(response.headers.get("Retry-After", 60))
                        logger.warning(f"Rate limited, waiting {retry_after}s")
                        await asyncio.sleep(retry_after)
                        continue
                    elif response.status in (401, 403):
                        logger.error(f"Auth error: {response.status}")
                        return {}
                    else:
                        error_body = await response.text()
                        logger.error(f"API error {response.status}: {error_body[:200]}")
                        if attempt < 2:
                            await asyncio.sleep(2 ** attempt)
                        continue
            except asyncio.TimeoutError:
                logger.warning(f"Request timeout (attempt {attempt+1}/3)")
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Request failed: {e}")
                if attempt < 2:
                    await asyncio.sleep(1)
        return {}

    async def get_user_by_username(self, username: str) -> Optional[Dict]:
        username = username.lstrip("@")
        result = await self._get(
            f"/users/by/username/{username}",
            params={"user.fields": "public_metrics,verified"}
        )
        return result.get("data") if "data" in result else None

    async def get_user_timeline(
        self, user_id: str, since_id: str = None, max_results: int = 10,
    ) -> List[Tweet]:
        params = {
            "max_results": min(max_results, 100),
            "tweet.fields": (
                "created_at,public_metrics,attachments,entities,"
                "referenced_tweets,lang,source"
            ),
            "user.fields": "username,name,verified,public_metrics",
            "media.fields": "type,url",
            "expansions": "author_id,attachments.media_keys,referenced_tweets.id",
            "exclude": "retweets",
        }
        if since_id:
            params["since_id"] = since_id

        result = await self._get(f"/users/{user_id}/tweets", params)
        return self._parse_response(result)

    async def search_recent(
        self, query: str, since_id: str = None, max_results: int = 10,
    ) -> List[Tweet]:
        params = {
            "query": query,
            "max_results": min(max_results, 100),
            "tweet.fields": (
                "created_at,public_metrics,attachments,entities,"
                "referenced_tweets,lang,source"
            ),
            "user.fields": "username,name,verified,public_metrics",
            "media.fields": "type,url",
            "expansions": "author_id,attachments.media_keys,referenced_tweets.id",
            "sort_order": "recency",
        }
        if since_id:
            params["since_id"] = since_id

        result = await self._get("/tweets/search/recent", params)
        return self._parse_response(result)

    # ==================== 批量采集（按账号池） ====================
    async def fetch_by_account(
        self, account: MonitoredAccount, since_id: str = None, max_results: int = 10,
    ) -> List[Tweet]:
        """获取指定账号的推文"""
        try:
            user = await self.get_user_by_username(account.username)
            if not user:
                logger.warning(f"User not found: @{account.username}")
                return []
            
            tweets = await self.get_user_timeline(
                user["id"], since_id=since_id, max_results=max_results
            )
            for t in tweets:
                t.source = f"tier:{account.tier}"
            
            if tweets:
                logger.debug(
                    f"  @{account.username} [{account.tier}]: {len(tweets)} tweets"
                )
            return tweets
        except Exception as e:
            logger.error(f"Error fetching @{account.username}: {e}")
            return []

    async def fetch_all_monitored(
        self,
        last_ids: Dict[str, str] = None,
        max_per_account: int = 10,
        tier_filter: Set[str] = None,
    ) -> Dict[str, List[Tweet]]:
        """
        按优先级批量采集所有监控账号

        参数:
            tier_filter: 只采集指定层级 (e.g., {"P1", "P2"})
        返回: {"@username": [Tweet, ...], ...}
        """
        if last_ids is None:
            last_ids = {}
        
        results: Dict[str, List[Tweet]] = {}
        
        # 按 P1 → P5 → ECO 顺序采集
        accounts_to_fetch = DEDUPED_ACCOUNTS
        if tier_filter:
            accounts_to_fetch = [
                a for a in accounts_to_fetch if a.tier in tier_filter
            ]
        
        # P1 优先并发
        tiers_ordered = ["P1", "P2", "P3", "P4", "P5", "ECO"]
        
        for tier in tiers_ordered:
            tier_accounts = [a for a in accounts_to_fetch if a.tier == tier]
            if not tier_accounts:
                continue
            
            # 同层级并发
            tasks = []
            for acc in tier_accounts:
                since_id = last_ids.get(acc.username.lower())
                tasks.append(
                    self.fetch_by_account(acc, since_id=since_id, max_results=max_per_account)
                )
            
            tier_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for acc, tweets in zip(tier_accounts, tier_results):
                if isinstance(tweets, Exception):
                    logger.error(f"  @{acc.username}: {tweets}")
                    continue
                if tweets:
                    results[acc.username] = tweets
        
        return results

    def _parse_response(self, response: Dict) -> List[Tweet]:
        if not response or "data" not in response:
            return []
        
        includes = response.get("includes", {})
        tweets = []
        for item in response["data"]:
            tweet = Tweet.from_api_response(item, includes)
            if tweet:
                tweets.append(tweet)
        return tweets


# ==================== Nitter 降级方案 ====================
class NitterFallback:
    NITTER_INSTANCES = [
        "https://nitter.net",
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
    ]

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "Mozilla/5.0 (compatible; OwlyBot/1.0)"}
        )

    async def stop(self):
        if self.session:
            await self.session.close()

    async def get_user_tweets(
        self, username: str, max_results: int = 10
    ) -> List[Tweet]:
        username = username.lstrip("@")
        tweets = []

        for instance in self.NITTER_INSTANCES:
            try:
                url = f"{instance}/{username}/rss"
                async with self.session.get(url) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        # 简单 RSS 解析（避免依赖外部 XML 库）
                        items = self._parse_rss_items(text, max_results)
                        for item in items:
                            tweet = Tweet(
                                tweet_id=item.get("id", ""),
                                author_id="",
                                author_username=username,
                                text=item.get("title", ""),
                                source="nitter_rss",
                                fetch_time=datetime.now(timezone.utc),
                            )
                            tweets.append(tweet)
                        
                        if tweets:
                            logger.info(f"Nitter: {len(tweets)} tweets for @{username}")
                            break
            except Exception as e:
                logger.debug(f"Nitter {instance} failed: {e}")
                continue

        return tweets

    def _parse_rss_items(self, xml_text: str, max_results: int) -> List[Dict]:
        """简单 RSS 解析"""
        results = []
        # 匹配 <item>...</item>
        item_pattern = r'<item>(.*?)</item>'
        title_pattern = r'<title>(.*?)</title>'
        link_pattern = r'<link>(.*?)</link>'

        items = __import__('re').findall(item_pattern, xml_text, __import__('re').DOTALL)
        for item_xml in items[:max_results]:
            title_match = __import__('re').search(title_pattern, item_xml)
            link_match = __import__('re').search(link_pattern, item_xml)
            
            if title_match:
                tid = link_match.group(1).split("/")[-1].replace("#m", "") if link_match else ""
                # 清理 HTML 实体
                title = title_match.group(1)
                title = title.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                title = title.replace("&quot;", '"').replace("&#39;", "'")
                results.append({"id": tid, "title": title})

        return results


    # ==================== 单用户便捷方法 ====================
    async def fetch_user_tweets(self, username: str, max_results: int = 5) -> List[Tweet]:
        """获取单个用户的最新推文（带 Nitter 降级）"""
        username = username.lstrip("@")
        try:
            user = await self.get_user_by_username(username)
            if user and user.get("id"):
                return await self.get_user_timeline(user["id"], max_results=max_results)
        except Exception:
            pass
        return []

    async def fetch_user_tweets_fallback(
        self, username: str, max_results: int = 5
    ) -> List[Tweet]:
        """获取单个用户推文（API+Nitter 双重保障）"""
        username = username.lstrip("@")
        # 先尝试 API
        tweets = await self.fetch_user_tweets(username, max_results)
        if tweets:
            return tweets
        # 降级 Nitter
        if not hasattr(self, "_nitter"):
            self._nitter = NitterFallback()
            await self._nitter.start()
        try:
            return await self._nitter.get_user_tweets(username, max_results)
        except Exception:
            return []


async def test():
    client = TwitterClient()
    await client.start()
    try:
        # 测试 P1 账号
        for acc in DEDUPED_ACCOUNTS[:3]:
            user = await client.get_user_by_username(acc.username)
            if user:
                tweets = await client.get_user_timeline(user["id"], max_results=2)
                print(f"@{acc.username} [{acc.tier}]: {len(tweets)} tweets")
    finally:
        await client.stop()


if __name__ == "__main__":
    asyncio.run(test())
