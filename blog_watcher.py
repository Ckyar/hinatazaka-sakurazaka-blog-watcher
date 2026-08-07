from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp
from bs4 import BeautifulSoup

HINATA_BASE_URL = "https://www.hinatazaka46.com"
HINATA_DETAIL_URL = HINATA_BASE_URL + "/s/official/diary/detail/{post_id}"
HINATA_INDEX_URL = HINATA_BASE_URL + "/s/official/diary/member?ima=0000"
HINATA_MEMBER_INDEX_URL = HINATA_BASE_URL + "/s/official/search/artist"
SAKURA_BASE_URL = "https://sakurazaka46.com"
SAKURA_DETAIL_URL = SAKURA_BASE_URL + "/s/s46/diary/detail/{post_id}?cd=blog"
SAKURA_INDEX_URL = SAKURA_BASE_URL + "/s/s46/diary/blog/list"
SAKURA_MEMBER_INDEX_URL = SAKURA_BASE_URL + "/s/s46/search/artist"
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True)
class BlogPost:
    post_id: int
    url: str
    title: str
    author: str
    published_at: str
    image_urls: tuple[str, ...]


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in items if item))


def safe_path_component(value: str, fallback: str, max_length: int = 100) -> str:
    """Create a readable component that is valid as a Windows folder/file name."""
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    if value.upper() in WINDOWS_RESERVED_NAMES:
        value = f"_{value}"
    return value[:max_length].rstrip(" .") or fallback


def post_date(published_at: str) -> str:
    match = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", published_at)
    if match is None:
        return "unknown-date"
    year, month, day = (int(part) for part in match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def post_directory(root: Path, post: BlogPost) -> Path:
    # Keep the complete path comfortably below the traditional Windows 260-char limit.
    member = safe_path_component(post.author, "unknown-member", 40)
    title = safe_path_component(post.title, f"blog-{post.post_id}", 50)
    folder = safe_path_component(
        f"{post_date(post.published_at)}_{title}_{post.post_id}",
        f"unknown-date_blog-{post.post_id}",
        85,
    )
    return root / member / folder


def parse_hinata_blog(post_id: int, html: str) -> BlogPost | None:
    """Return a blog post only when the detail page is genuinely a blog."""
    if not html.strip() or "ブログ" not in html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("div.l-maincontents--blog")
    article = main.select_one("div.p-blog-article") if main else None
    body = article.select_one("div.c-blog-article__text") if article else None
    if body is None:
        return None

    image_urls = _unique(
        urljoin(HINATA_BASE_URL, image.get("src", "").strip())
        for image in body.select("img[src]")
    )
    title_el = article.select_one("div.c-blog-article__title")
    author_el = article.select_one("div.c-blog-article__name")
    time_el = article.select_one("time")

    return BlogPost(
        post_id=post_id,
        url=HINATA_DETAIL_URL.format(post_id=post_id),
        title=title_el.get_text(" ", strip=True) if title_el else f"Blog #{post_id}",
        author=author_el.get_text(" ", strip=True) if author_el else "",
        published_at=time_el.get_text(" ", strip=True) if time_el else "",
        image_urls=image_urls,
    )


def parse_sakura_blog(post_id: int, html: str) -> BlogPost | None:
    if not html.strip() or "ブログ" not in html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    article = soup.select_one("article.post")
    body = article.select_one("div.box-article") if article else None
    if body is None:
        return None

    image_urls = _unique(
        urljoin(SAKURA_BASE_URL, image.get("src", "").strip())
        for image in body.select("img[src]")
    )
    title_el = article.select_one(".title")
    author_el = article.select_one(".blog-foot .name")
    time_el = article.select_one(".blog-foot .date")
    title = title_el.get_text(" ", strip=True) if title_el else ""

    return BlogPost(
        post_id=post_id,
        url=SAKURA_DETAIL_URL.format(post_id=post_id),
        title=title or "無題",
        author=author_el.get_text(" ", strip=True) if author_el else "",
        published_at=time_el.get_text(" ", strip=True) if time_el else "",
        image_urls=image_urls,
    )


def parse_blog_index(html: str) -> tuple[int, ...]:
    """Extract unique detail IDs in the publication order shown by the homepage."""
    if not html.strip():
        return ()
    soup = BeautifulSoup(html, "html.parser")
    post_ids: list[int] = []
    seen: set[int] = set()
    for anchor in soup.select('a[href*="/diary/detail/"]'):
        match = re.search(r"/diary/detail/(\d+)", anchor.get("href", ""))
        if match is None:
            continue
        post_id = int(match.group(1))
        if post_id not in seen:
            seen.add(post_id)
            post_ids.append(post_id)
    return tuple(post_ids)


def parse_member_names(html: str, group_key: str) -> tuple[str, ...]:
    """Extract current member display names from an official member page."""
    if not html.strip():
        return ()
    selectors = {
        "hinata": 'a[href*="/s/official/artist/"] .c-member__name',
        "sakura": 'a[href*="/s/s46/artist/"] p.name',
    }
    try:
        selector = selectors[group_key]
    except KeyError as error:
        raise ValueError(f"Unknown member group: {group_key}") from error

    soup = BeautifulSoup(html, "html.parser")
    names = (
        re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()
        for element in soup.select(selector)
    )
    return _unique(names)


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sent_images (
                post_id INTEGER NOT NULL,
                image_url TEXT NOT NULL,
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (post_id, image_url)
            );
            CREATE TABLE IF NOT EXISTS announced_posts (
                post_id INTEGER PRIMARY KEY,
                announced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS completed_posts (
                post_id INTEGER PRIMARY KEY,
                completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self._set_default("index_initialized", "0")
        self.connection.commit()

    def _set_default(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
        )

    def _get_int(self, key: str) -> int:
        row = self.connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Missing state setting: {key}")
        return int(row[0])

    def _set_int(self, key: str, value: int) -> None:
        self.connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self.connection.commit()

    def post_announced(self, post_id: int) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM announced_posts WHERE post_id = ?", (post_id,)
        ).fetchone() is not None

    def mark_post_announced(self, post_id: int) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO announced_posts(post_id) VALUES (?)", (post_id,)
        )
        self.connection.commit()

    def image_sent(self, post_id: int, image_url: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM sent_images WHERE post_id = ? AND image_url = ?",
            (post_id, image_url),
        ).fetchone() is not None

    def mark_image_sent(self, post_id: int, image_url: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO sent_images(post_id, image_url) VALUES (?, ?)",
            (post_id, image_url),
        )
        self.connection.commit()

    @property
    def index_initialized(self) -> bool:
        return bool(self._get_int("index_initialized"))

    def mark_index_initialized(self) -> None:
        self._set_int("index_initialized", 1)

    def post_completed(self, post_id: int) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM completed_posts WHERE post_id = ?", (post_id,)
        ).fetchone() is not None

    def mark_post_completed(self, post_id: int) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO completed_posts(post_id) VALUES (?)", (post_id,)
        )
        self.connection.commit()

    def enable_previously_ignored_posts(self) -> int:
        """One-time migration: reactivate baseline items that were never announced."""
        migration_key = "existing_homepage_posts_enabled"
        row = self.connection.execute(
            "SELECT 1 FROM settings WHERE key = ?", (migration_key,)
        ).fetchone()
        if row is not None:
            return 0
        cursor = self.connection.execute(
            "DELETE FROM completed_posts "
            "WHERE post_id NOT IN (SELECT post_id FROM announced_posts)"
        )
        self.connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, '1')", (migration_key,)
        )
        self.connection.commit()
        return cursor.rowcount

    def close(self) -> None:
        self.connection.close()


class SubscriptionStore:
    """Persist per-server subscriptions independently from blog processing state."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                group_key TEXT NOT NULL,
                member_key TEXT NOT NULL,
                member_name TEXT NOT NULL,
                subscribed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, user_id, group_key, member_key)
            );
            CREATE INDEX IF NOT EXISTS subscriptions_lookup
                ON subscriptions(guild_id, group_key, member_key);
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def subscribe(
        self,
        guild_id: int,
        user_id: int,
        group_key: str,
        member_key: str,
        member_name: str,
    ) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO subscriptions "
            "(guild_id, user_id, group_key, member_key, member_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, group_key, member_key, member_name),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def unsubscribe(
        self, guild_id: int, user_id: int, group_key: str, member_key: str
    ) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM subscriptions "
            "WHERE guild_id = ? AND user_id = ? AND group_key = ? AND member_key = ?",
            (guild_id, user_id, group_key, member_key),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def subscriber_ids(
        self, guild_id: int, group_key: str, member_key: str
    ) -> tuple[int, ...]:
        rows = self.connection.execute(
            "SELECT user_id FROM subscriptions "
            "WHERE guild_id = ? AND group_key = ? AND member_key = ? "
            "ORDER BY user_id",
            (guild_id, group_key, member_key),
        ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def user_subscriptions(
        self, guild_id: int, user_id: int
    ) -> tuple[tuple[str, str], ...]:
        rows = self.connection.execute(
            "SELECT group_key, member_name FROM subscriptions "
            "WHERE guild_id = ? AND user_id = ? "
            "ORDER BY group_key, member_name",
            (guild_id, user_id),
        ).fetchall()
        return tuple((str(row[0]), str(row[1])) for row in rows)

    def user_group_subscriptions(
        self, guild_id: int, user_id: int, group_key: str
    ) -> tuple[tuple[str, str], ...]:
        rows = self.connection.execute(
            "SELECT member_key, member_name FROM subscriptions "
            "WHERE guild_id = ? AND user_id = ? AND group_key = ? "
            "ORDER BY member_name",
            (guild_id, user_id, group_key),
        ).fetchall()
        return tuple((str(row[0]), str(row[1])) for row in rows)

    def replace_page_subscriptions(
        self,
        guild_id: int,
        user_id: int,
        group_key: str,
        page_members: tuple[tuple[str, str], ...],
        selected_member_keys: set[str],
    ) -> None:
        """Replace one GUI page atomically without affecting other pages/groups."""
        page_member_map = dict(page_members)
        invalid_keys = selected_member_keys.difference(page_member_map)
        if invalid_keys:
            raise ValueError("selected_member_keys contains members outside this page")
        if not page_members:
            return

        placeholders = ",".join("?" for _ in page_members)
        page_keys = tuple(page_member_map)
        with self.connection:
            self.connection.execute(
                "DELETE FROM subscriptions "
                "WHERE guild_id = ? AND user_id = ? AND group_key = ? "
                f"AND member_key IN ({placeholders})",
                (guild_id, user_id, group_key, *page_keys),
            )
            self.connection.executemany(
                "INSERT INTO subscriptions "
                "(guild_id, user_id, group_key, member_key, member_name) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        guild_id,
                        user_id,
                        group_key,
                        member_key,
                        page_member_map[member_key],
                    )
                    for member_key in page_keys
                    if member_key in selected_member_keys
                ),
            )

    def get_setting(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ).fetchone()
        return str(row[0]) if row is not None else None

    def set_setting(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO bot_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.connection.commit()

    def delete_setting(self, key: str) -> None:
        self.connection.execute("DELETE FROM bot_settings WHERE key = ?", (key,))
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class BlogFetcher:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        detail_url_template: str,
        source_name: str,
        timeout_seconds: float,
        retries: int,
    ) -> None:
        self.session = session
        self.detail_url_template = detail_url_template
        self.source_name = source_name
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.retries = retries
        self.log = logging.getLogger(__name__)

    async def fetch(self, post_id: int) -> str:
        url = self.detail_url_template.format(post_id=post_id)
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                async with self.session.get(url, timeout=self.timeout) as response:
                    if response.status == 404:
                        return ""
                    response.raise_for_status()
                    return await response.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                last_error = error
                self.log.warning(
                    "%s detail attempt %s/%s failed for ID %s: %s",
                    self.source_name,
                    attempt,
                    self.retries,
                    post_id,
                    error,
                )
                if attempt < self.retries:
                    await asyncio.sleep(2 ** (attempt - 1))
        raise RuntimeError(
            f"Unable to fetch {self.source_name} blog ID {post_id}"
        ) from last_error


class BlogIndexFetcher:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        index_url: str,
        source_name: str,
        timeout_seconds: float,
        retries: int,
    ) -> None:
        self.session = session
        self.index_url = index_url
        self.source_name = source_name
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.retries = retries
        self.log = logging.getLogger(__name__)

    async def fetch(self) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                async with self.session.get(
                    self.index_url, timeout=self.timeout
                ) as response:
                    response.raise_for_status()
                    return await response.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                last_error = error
                self.log.warning(
                    "%s homepage attempt %s/%s failed: %s",
                    self.source_name,
                    attempt,
                    self.retries,
                    error,
                )
                if attempt < self.retries:
                    await asyncio.sleep(2 ** (attempt - 1))
        raise RuntimeError(
            f"Unable to fetch {self.source_name} official blog homepage"
        ) from last_error


class BlogImageDownloader:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        root: Path,
        timeout_seconds: float,
        retries: int,
    ) -> None:
        self.session = session
        self.root = root
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.retries = retries
        self.log = logging.getLogger(__name__)

    @staticmethod
    def _filename(index: int, image_url: str) -> str:
        url_name = unquote(Path(urlsplit(image_url).path).name)
        parsed_name = Path(url_name)
        suffix = parsed_name.suffix.lower()
        if re.fullmatch(r"\.[a-z0-9]{1,9}", suffix) is None:
            suffix = ".jpg"
        stem = safe_path_component(parsed_name.stem, f"image-{index:02d}", 45)
        return f"{index:02d}_{stem}{suffix}"

    async def download_post(self, post: BlogPost) -> tuple[Path, ...]:
        directory = post_directory(self.root, post)
        directory.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []
        for index, image_url in enumerate(post.image_urls, start=1):
            target = directory / self._filename(index, image_url)
            if target.is_file() and target.stat().st_size > 0:
                downloaded.append(target)
                continue
            await self._download(image_url, target)
            downloaded.append(target)
        return tuple(downloaded)

    async def _download(self, image_url: str, target: Path) -> None:
        temporary = target.with_name(target.name + ".part")
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                async with self.session.get(image_url, timeout=self.timeout) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as output:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            output.write(chunk)
                temporary.replace(target)
                self.log.info("Image saved: %s", target)
                return
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
                last_error = error
                temporary.unlink(missing_ok=True)
                self.log.warning(
                    "Image download attempt %s/%s failed for %s: %s",
                    attempt,
                    self.retries,
                    image_url,
                    error,
                )
                if attempt < self.retries:
                    await asyncio.sleep(2 ** (attempt - 1))
        raise RuntimeError(f"Unable to download image: {image_url}") from last_error
