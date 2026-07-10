from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

import aiohttp
import discord
from dotenv import load_dotenv

from blog_watcher import (
    BlogFetcher,
    BlogIndexFetcher,
    BlogImageDownloader,
    BlogPost,
    HINATA_DETAIL_URL,
    HINATA_INDEX_URL,
    SAKURA_DETAIL_URL,
    SAKURA_INDEX_URL,
    StateStore,
    parse_hinata_blog,
    parse_blog_index,
    parse_sakura_blog,
)

ROOT = Path(__file__).resolve().parent


def image_batches(image_urls: list[str], size: int = 10) -> list[list[str]]:
    return [image_urls[index : index + size] for index in range(0, len(image_urls), size)]


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} 必須是整數，目前是 {raw!r}") from error
    if value <= 0:
        raise ValueError(f"{name} 必須大於 0")
    return value


@dataclass(frozen=True)
class SourceConfig:
    key: str
    name: str
    channel_id: int
    index_url: str
    detail_url: str
    parser: Callable[[int, str], BlogPost | None]
    interval: int
    initial_delay: int
    state_db: Path
    image_dir: Path


@dataclass(frozen=True)
class Config:
    token: str
    sources: tuple[SourceConfig, ...]
    timeout: int
    retries: int
    user_agent: str
    discord_history_limit: int
    status_log_interval: int

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        if not token or token == "請填入Bot_Token":
            raise ValueError("請在 .env 設定 DISCORD_BOT_TOKEN")

        def channel_id(name: str) -> int:
            raw = os.getenv(name, "").strip()
            if not raw.isdigit():
                raise ValueError(f"請在 .env 設定數字格式的 {name}")
            return int(raw)

        def local_path(name: str, default: str) -> Path:
            path = Path(os.getenv(name, default))
            return path if path.is_absolute() else ROOT / path

        interval = _positive_int("CHECK_INTERVAL_SECONDS", 15)
        sources = (
            SourceConfig(
                key="hinata",
                name="日向坂46",
                channel_id=channel_id("DISCORD_CHANNEL_ID"),
                index_url=HINATA_INDEX_URL,
                detail_url=HINATA_DETAIL_URL,
                parser=parse_hinata_blog,
                interval=interval,
                initial_delay=0,
                state_db=local_path("STATE_DB", "data/watcher.db"),
                image_dir=local_path("IMAGE_DIR", "images/日向坂46"),
            ),
            SourceConfig(
                key="sakura",
                name="櫻坂46",
                channel_id=channel_id("SAKURA_DISCORD_CHANNEL_ID"),
                index_url=SAKURA_INDEX_URL,
                detail_url=SAKURA_DETAIL_URL,
                parser=parse_sakura_blog,
                interval=_positive_int("SAKURA_CHECK_INTERVAL_SECONDS", 15),
                initial_delay=5,
                state_db=local_path("SAKURA_STATE_DB", "data/sakura_watcher.db"),
                image_dir=local_path("SAKURA_IMAGE_DIR", "images/櫻坂46"),
            ),
        )
        return cls(
            token=token,
            sources=sources,
            timeout=_positive_int("HTTP_TIMEOUT_SECONDS", 20),
            retries=_positive_int("HTTP_RETRIES", 3),
            user_agent=os.getenv("USER_AGENT", "SakamichiBlogWatcher/1.0"),
            discord_history_limit=_positive_int("DISCORD_HISTORY_LIMIT", 500),
            status_log_interval=_positive_int("STATUS_LOG_INTERVAL_SECONDS", 300),
        )


def setup_logging() -> None:
    log_path = Path(os.getenv("LOG_FILE", "logs/watcher.log"))
    if not log_path.is_absolute():
        log_path = ROOT / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    file_handler = RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[file_handler, console_handler])


class MultiBlogWatcher(discord.Client):
    def __init__(self, config: Config) -> None:
        super().__init__(intents=discord.Intents.none())
        self.config = config
        self.states = {
            source.key: StateStore(source.state_db)
            for source in config.sources
        }
        self.reactivated_posts = self.states["sakura"].enable_previously_ignored_posts()
        self.http_session: aiohttp.ClientSession | None = None
        self.workers: dict[str, asyncio.Task[None]] = {}
        self.log = logging.getLogger("watcher")

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(
            headers={"User-Agent": self.config.user_agent}
        )

    async def on_ready(self) -> None:
        self.log.info("Discord connected as %s", self.user)
        if self.reactivated_posts:
            self.log.info(
                "櫻坂46 reactivated %s previously ignored homepage item(s)",
                self.reactivated_posts,
            )
        for source in self.config.sources:
            self.log.info(
                "%s watcher targets Discord channel %s",
                source.name,
                source.channel_id,
            )
            worker = self.workers.get(source.key)
            if worker is None or worker.done():
                self.workers[source.key] = asyncio.create_task(
                    self.watch_forever(source, self.states[source.key]),
                    name=f"blog-watcher-{source.key}",
                )

    async def get_target_channel(
        self, source: SourceConfig
    ) -> discord.abc.Messageable:
        channel = self.get_channel(source.channel_id)
        if channel is None:
            channel = await self.fetch_channel(source.channel_id)
        if not hasattr(channel, "send"):
            raise RuntimeError(
                f"{source.name} 的頻道 ID {source.channel_id} 不是可傳送訊息的頻道"
            )
        return channel  # type: ignore[return-value]

    @staticmethod
    def already_sent(state: StateStore, post: BlogPost) -> bool:
        return state.post_announced(post.post_id) and all(
            state.image_sent(post.post_id, image_url)
            for image_url in post.image_urls
        )

    async def reconcile_discord_history(
        self,
        source: SourceConfig,
        state: StateStore,
        channel: discord.abc.Messageable,
        post: BlogPost,
    ) -> None:
        """Recover deduplication state from messages previously sent by this bot."""
        if self.user is None:
            return
        need_post = not state.post_announced(post.post_id)
        missing_images = {
            image_url
            for image_url in post.image_urls
            if not state.image_sent(post.post_id, image_url)
        }
        if not need_post and not missing_images:
            return

        try:
            async for message in channel.history(
                limit=self.config.discord_history_limit
            ):
                if message.author.id != self.user.id:
                    continue
                content = message.content
                if need_post and post.url in content:
                    state.mark_post_announced(post.post_id)
                    need_post = False
                    self.log.info(
                        "%s recovered blog ID %s from Discord history",
                        source.name,
                        post.post_id,
                    )
                embedded_images = {
                    embed.image.url
                    for embed in message.embeds
                    if embed.image and embed.image.url
                }
                matched_images = {
                    image_url
                    for image_url in missing_images
                    if image_url in content or image_url in embedded_images
                }
                for image_url in matched_images:
                    state.mark_image_sent(post.post_id, image_url)
                missing_images.difference_update(matched_images)
                if not need_post and not missing_images:
                    break
        except discord.Forbidden:
            self.log.warning(
                "%s cannot read Discord history; grant Read Message History to enable "
                "server-side duplicate checking. Local SQLite deduplication remains active.",
                source.name,
            )

    async def announce(
        self, source: SourceConfig, state: StateStore, post: BlogPost
    ) -> None:
        if self.already_sent(state, post):
            state.mark_post_completed(post.post_id)
            self.log.info(
                "%s blog ID %s was already sent; skipping Discord",
                source.name,
                post.post_id,
            )
            return

        channel = await self.get_target_channel(source)
        await self.reconcile_discord_history(source, state, channel, post)
        if self.already_sent(state, post):
            state.mark_post_completed(post.post_id)
            self.log.info(
                "%s blog ID %s already exists in Discord history; skipping",
                source.name,
                post.post_id,
            )
            return

        needs_heading = not state.post_announced(post.post_id)
        heading = f"🆕 **[{source.name}] {post.title}**"
        details = " · ".join(part for part in (post.author, post.published_at) if part)
        heading_content = f"{heading}\n{details}\n<{post.url}>"
        missing_images = [
            image_url
            for image_url in post.image_urls
            if not state.image_sent(post.post_id, image_url)
        ]

        if not missing_images:
            if needs_heading:
                await channel.send(heading_content)
                state.mark_post_announced(post.post_id)
            state.mark_post_completed(post.post_id)
            self.log.info(
                "%s blog ID %s has no body images", source.name, post.post_id
            )
            return

        # Discord accepts at most 10 rich embeds in a single message.
        batches = image_batches(missing_images)
        for batch_number, image_urls in enumerate(batches, start=1):
            embeds = [discord.Embed().set_image(url=image_url) for image_url in image_urls]
            content = heading_content if needs_heading and batch_number == 1 else None
            await channel.send(content=content, embeds=embeds)
            if content is not None:
                state.mark_post_announced(post.post_id)
            for image_url in image_urls:
                state.mark_image_sent(post.post_id, image_url)
            if batch_number < len(batches):
                await asyncio.sleep(1)

        self.log.info(
            "%s blog ID %s sent in %s Discord message(s) with %s image embed(s)",
            source.name,
            post.post_id,
            len(batches),
            len(missing_images),
        )
        state.mark_post_completed(post.post_id)

    async def watch_forever(
        self, source: SourceConfig, state: StateStore
    ) -> None:
        assert self.http_session is not None
        fetcher = BlogFetcher(
            self.http_session,
            source.detail_url,
            source.name,
            self.config.timeout,
            self.config.retries,
        )
        index_fetcher = BlogIndexFetcher(
            self.http_session,
            source.index_url,
            source.name,
            self.config.timeout,
            self.config.retries,
        )
        downloader = BlogImageDownloader(
            self.http_session,
            source.image_dir,
            self.config.timeout,
            self.config.retries,
        )
        if source.initial_delay:
            await asyncio.sleep(source.initial_delay)
        last_status_log = 0.0
        while not self.is_closed():
            try:
                index_html = await index_fetcher.fetch()
                post_ids = parse_blog_index(index_html)
                if not post_ids:
                    raise RuntimeError(
                        f"{source.name} homepage contained no detail links"
                    )

                if not state.index_initialized:
                    state.mark_index_initialized()
                    self.log.info(
                        "%s homepage mode initialized; current items are pending",
                        source.name,
                    )

                # Homepage order is newest first. Reverse it so Discord receives old-to-new.
                pending_ids = [
                    post_id
                    for post_id in reversed(post_ids)
                    if not state.post_completed(post_id)
                ]
                now = asyncio.get_running_loop().time()
                if pending_ids or now - last_status_log >= self.config.status_log_interval:
                    self.log.info(
                        "%s homepage poll found %s article(s); %s pending",
                        source.name,
                        len(post_ids),
                        len(pending_ids),
                    )
                    last_status_log = now

                for post_id in pending_ids:
                    try:
                        html = await fetcher.fetch(post_id)
                        post = source.parser(post_id, html)
                        if post is None:
                            self.log.warning(
                                "%s homepage linked ID %s, but detail validation failed",
                                source.name,
                                post_id,
                            )
                            continue
                        self.log.info(
                            "%s blog ID %s found with %s image(s): %s",
                            source.name,
                            post_id,
                            len(post.image_urls),
                            post.title,
                        )
                        saved_images = await downloader.download_post(post)
                        self.log.info(
                            "%s blog ID %s stored in %s (%s image(s))",
                            source.name,
                            post_id,
                            saved_images[0].parent
                            if saved_images
                            else source.image_dir,
                            len(saved_images),
                        )
                        await self.announce(source, state, post)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self.log.exception(
                            "%s processing failed for blog ID %s; will retry",
                            source.name,
                            post_id,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("%s homepage poll failed; will retry", source.name)
            await asyncio.sleep(source.interval)

    async def close(self) -> None:
        workers = list(self.workers.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        if self.http_session is not None:
            await self.http_session.close()
        for state in self.states.values():
            state.close()
        await super().close()


# Keep the original class name import-compatible for existing tests/users.
HinataWatcher = MultiBlogWatcher


def main() -> int:
    load_dotenv(ROOT / ".env")
    setup_logging()
    try:
        config = Config.from_env()
    except ValueError as error:
        logging.error("設定錯誤：%s", error)
        return 2

    client = MultiBlogWatcher(config)
    try:
        client.run(config.token, log_handler=None)
    except discord.LoginFailure:
        logging.error("Discord Bot Token 無效")
        return 3
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
