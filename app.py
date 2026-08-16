from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import unicodedata
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
    HINATA_MEMBER_INDEX_URL,
    SAKURA_DETAIL_URL,
    SAKURA_INDEX_URL,
    SAKURA_MEMBER_INDEX_URL,
    StateStore,
    SubscriptionStore,
    parse_hinata_blog,
    parse_blog_index,
    parse_member_names,
    parse_sakura_blog,
)
from toyoko_watcher import (
    JAPAN_PREFECTURE_REGIONS,
    SMOKING_LABELS,
    ToyokoAvailabilityResult,
    ToyokoClient,
    ToyokoHTTPError,
    ToyokoRule,
    ToyokoStore,
    build_toyoko_search_url,
    japan_today,
    parse_interval_minutes,
    parse_toyoko_search_url,
    should_notify_availability,
)

ROOT = Path(__file__).resolve().parent

SPECIAL_SUBSCRIPTION_AUTHORS: dict[str, dict[str, tuple[str, ...]]] = {
    "hinata": {
        "ポカ": ("ぽか", "poka"),
    },
}


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


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"{name} 必須是 true/false、yes/no、on/off 或 1/0，目前是 {raw!r}"
    )


def _normalize_member_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _member_lookup_key(value: str) -> str:
    return "".join(_normalize_member_name(value).split()).casefold()


def subscription_author_catalog(
    group_key: str, member_names: tuple[str, ...]
) -> dict[str, str]:
    catalog = {_member_lookup_key(name): name for name in member_names}
    for canonical_name, aliases in SPECIAL_SUBSCRIPTION_AUTHORS.get(
        group_key, {}
    ).items():
        for name in (canonical_name, *aliases):
            catalog[_member_lookup_key(name)] = canonical_name
    return catalog


def _split_member_names(value: str) -> tuple[str, ...]:
    """Split a batch while preserving spaces that are part of member names."""
    names: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[,，、;；\r\n]+", value):
        name = _normalize_member_name(item)
        key = _member_lookup_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return tuple(names)


def _member_mentions(name: str) -> dict[str, tuple[int, ...]]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"{name} 必須是有效的單行 JSON object") from error
    if not isinstance(configured, dict):
        raise ValueError(f"{name} 必須是成員名字對 Discord 使用者 ID 的 JSON object")

    mentions: dict[str, tuple[int, ...]] = {}
    for member, configured_ids in configured.items():
        if not isinstance(member, str) or not _normalize_member_name(member):
            raise ValueError(f"{name} 包含無效的成員名字")
        if isinstance(configured_ids, (str, int)):
            configured_ids = [configured_ids]
        if not isinstance(configured_ids, list) or not configured_ids:
            raise ValueError(f"{name} 的 {member!r} 必須至少設定一個使用者 ID")

        user_ids: list[int] = []
        for configured_id in configured_ids:
            user_id = str(configured_id).strip()
            if not user_id.isdigit() or int(user_id) <= 0:
                raise ValueError(
                    f"{name} 的 {member!r} 包含無效的 Discord 使用者 ID"
                )
            numeric_id = int(user_id)
            if numeric_id not in user_ids:
                user_ids.append(numeric_id)
        mentions[_member_lookup_key(member)] = tuple(user_ids)
    return mentions


def _optional_channel_id(name: str) -> int:
    raw = os.getenv(name, "").strip()
    if not raw or raw == "0":
        return 0
    if not raw.isdigit():
        raise ValueError(f"{name} 必須是數字格式的 Discord 頻道 ID")
    return int(raw)


@dataclass(frozen=True)
class SourceConfig:
    key: str
    name: str
    channel_id: int
    index_url: str
    detail_url: str
    member_index_url: str
    parser: Callable[[int, str], BlogPost | None]
    interval: int
    initial_delay: int
    state_db: Path
    image_dir: Path
    member_mentions: dict[str, tuple[int, ...]]
    enabled: bool


def member_mention_ids(source: SourceConfig, author: str) -> tuple[int, ...]:
    return source.member_mentions.get(_member_lookup_key(author), ())


@dataclass(frozen=True)
class Config:
    token: str
    sources: tuple[SourceConfig, ...]
    timeout: int
    retries: int
    user_agent: str
    discord_history_limit: int
    status_log_interval: int
    save_images_locally: bool
    subscription_channel_id: int
    subscription_db: Path
    member_catalog_refresh_seconds: int
    enable_subscription_gui: bool
    enable_text_subscription_commands: bool
    pin_subscription_panel: bool
    enable_toyoko_watcher: bool
    toyoko_channel_id: int
    toyoko_db: Path
    toyoko_default_min_interval_seconds: int
    toyoko_default_max_interval_seconds: int
    toyoko_min_allowed_interval_seconds: int
    toyoko_request_gap_seconds: int
    toyoko_rule_gap_seconds: int
    toyoko_max_rules_per_user: int
    pin_toyoko_panel: bool

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        if not token or token == "請填入Bot_Token":
            raise ValueError("請在 .env 設定 DISCORD_BOT_TOKEN")

        def channel_id(name: str, *, required: bool = True) -> int:
            raw = os.getenv(name, "").strip()
            if not required and (not raw or raw == "0"):
                return 0
            if not raw.isdigit():
                raise ValueError(f"請在 .env 設定數字格式的 {name}")
            return int(raw)

        def local_path(name: str, default: str) -> Path:
            path = Path(os.getenv(name, default))
            return path if path.is_absolute() else ROOT / path

        interval = _positive_int("HINATA_CHECK_INTERVAL_SECONDS", 15)
        hinata_enabled = _boolean("HINATA_ENABLE_WATCHER", True)
        sakura_enabled = _boolean("SAKURA_ENABLE_WATCHER", True)
        sources = (
            SourceConfig(
                key="hinata",
                name="日向坂46",
                channel_id=channel_id(
                    "HINATA_DISCORD_CHANNEL_ID", required=hinata_enabled
                ),
                index_url=HINATA_INDEX_URL,
                detail_url=HINATA_DETAIL_URL,
                member_index_url=HINATA_MEMBER_INDEX_URL,
                parser=parse_hinata_blog,
                interval=interval,
                initial_delay=0,
                state_db=local_path("HINATA_STATE_DB", "data/watcher.db"),
                image_dir=local_path("HINATA_IMAGE_DIR", "images/日向坂46"),
                member_mentions=_member_mentions("HINATA_MEMBER_MENTIONS"),
                enabled=hinata_enabled,
            ),
            SourceConfig(
                key="sakura",
                name="櫻坂46",
                channel_id=channel_id(
                    "SAKURA_DISCORD_CHANNEL_ID", required=sakura_enabled
                ),
                index_url=SAKURA_INDEX_URL,
                detail_url=SAKURA_DETAIL_URL,
                member_index_url=SAKURA_MEMBER_INDEX_URL,
                parser=parse_sakura_blog,
                interval=_positive_int("SAKURA_CHECK_INTERVAL_SECONDS", 15),
                initial_delay=5,
                state_db=local_path("SAKURA_STATE_DB", "data/sakura_watcher.db"),
                image_dir=local_path("SAKURA_IMAGE_DIR", "images/櫻坂46"),
                member_mentions=_member_mentions("SAKURA_MEMBER_MENTIONS"),
                enabled=sakura_enabled,
            ),
        )
        enable_toyoko_watcher = _boolean("ENABLE_TOYOKO_WATCHER", False)
        toyoko_channel_id = _optional_channel_id("TOYOKO_CHANNEL_ID")
        if enable_toyoko_watcher and not toyoko_channel_id:
            raise ValueError(
                "ENABLE_TOYOKO_WATCHER=true 時必須設定 TOYOKO_CHANNEL_ID"
            )
        toyoko_default_min_interval_seconds = _positive_int(
            "TOYOKO_DEFAULT_MIN_INTERVAL_SECONDS", 600
        )
        toyoko_default_max_interval_seconds = _positive_int(
            "TOYOKO_DEFAULT_MAX_INTERVAL_SECONDS", 900
        )
        if (
            toyoko_default_max_interval_seconds
            < toyoko_default_min_interval_seconds
        ):
            raise ValueError(
                "TOYOKO_DEFAULT_MAX_INTERVAL_SECONDS 不得小於 "
                "TOYOKO_DEFAULT_MIN_INTERVAL_SECONDS"
            )
        toyoko_min_allowed_interval_seconds = _positive_int(
            "TOYOKO_MIN_ALLOWED_INTERVAL_SECONDS", 600
        )
        if (
            toyoko_default_min_interval_seconds
            < toyoko_min_allowed_interval_seconds
        ):
            raise ValueError(
                "TOYOKO_DEFAULT_MIN_INTERVAL_SECONDS 不得小於 "
                "TOYOKO_MIN_ALLOWED_INTERVAL_SECONDS"
            )
        return cls(
            token=token,
            sources=sources,
            timeout=_positive_int("HTTP_TIMEOUT_SECONDS", 20),
            retries=_positive_int("HTTP_RETRIES", 3),
            user_agent=os.getenv("USER_AGENT", "SakamichiBlogWatcher/1.0"),
            discord_history_limit=_positive_int("DISCORD_HISTORY_LIMIT", 500),
            status_log_interval=_positive_int("STATUS_LOG_INTERVAL_SECONDS", 300),
            save_images_locally=_boolean("SAVE_IMAGES_LOCALLY", True),
            subscription_channel_id=_optional_channel_id("SUBSCRIPTION_CHANNEL_ID"),
            subscription_db=local_path(
                "SUBSCRIPTION_DB", "data/subscriptions.db"
            ),
            member_catalog_refresh_seconds=_positive_int(
                "MEMBER_CATALOG_REFRESH_SECONDS", 3600
            ),
            enable_subscription_gui=_boolean("ENABLE_SUBSCRIPTION_GUI", True),
            enable_text_subscription_commands=_boolean(
                "ENABLE_TEXT_SUBSCRIPTION_COMMANDS", True
            ),
            pin_subscription_panel=_boolean("PIN_SUBSCRIPTION_PANEL", True),
            enable_toyoko_watcher=enable_toyoko_watcher,
            toyoko_channel_id=toyoko_channel_id,
            toyoko_db=local_path("TOYOKO_DB", "data/toyoko_watcher.db"),
            toyoko_default_min_interval_seconds=(
                toyoko_default_min_interval_seconds
            ),
            toyoko_default_max_interval_seconds=(
                toyoko_default_max_interval_seconds
            ),
            toyoko_min_allowed_interval_seconds=(
                toyoko_min_allowed_interval_seconds
            ),
            toyoko_request_gap_seconds=_positive_int(
                "TOYOKO_REQUEST_GAP_SECONDS", 3
            ),
            toyoko_rule_gap_seconds=_positive_int(
                "TOYOKO_RULE_GAP_SECONDS", 30
            ),
            toyoko_max_rules_per_user=_positive_int(
                "TOYOKO_MAX_RULES_PER_USER", 5
            ),
            pin_toyoko_panel=_boolean("PIN_TOYOKO_PANEL", True),
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


SUBSCRIPTION_PAGE_SIZE = 20
SUBSCRIPTION_PANEL_CONTENT = (
    "## 🌸 部落格關注管理\n"
    "按下按鈕後，只有你看得到自己的管理畫面。你可以分別選擇日向坂46或"
    "櫻坂46成員；關注的成員發表部落格時，POKA 會在圖片貼文中標記你。"
)


def _unique_catalog_members(catalog: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """Return canonical member keys/names once, preserving official page order."""
    members: list[tuple[str, str]] = []
    seen: set[str] = set()
    for member_name in catalog.values():
        member_key = _member_lookup_key(member_name)
        if member_key in seen:
            continue
        seen.add(member_key)
        members.append((member_key, member_name))
    return tuple(members)


class SubscriptionView(discord.ui.View):
    """Common error handling for every subscription interaction."""

    def __init__(
        self, watcher: "MultiBlogWatcher", *, timeout: float | None
    ) -> None:
        super().__init__(timeout=timeout)
        self.watcher = watcher

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[discord.ui.View],
    ) -> None:
        self.watcher.log.error(
            "Subscription interaction failed for custom_id=%s user=%s guild=%s",
            getattr(item, "custom_id", None),
            interaction.user.id,
            interaction.guild_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        error_message = "POKA 處理關注操作時發生錯誤，請重新開啟關注管理面板再試一次。"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(error_message, ephemeral=True)
            else:
                await interaction.response.send_message(
                    error_message, ephemeral=True
                )
        except discord.HTTPException:
            self.watcher.log.exception(
                "Unable to send subscription interaction error response"
            )


class SubscriptionPanelView(SubscriptionView):
    """Persistent public entry point; the actual manager is always ephemeral."""

    def __init__(self, watcher: "MultiBlogWatcher") -> None:
        super().__init__(watcher, timeout=None)

    @discord.ui.button(
        label="管理我的關注",
        emoji="⚙️",
        style=discord.ButtonStyle.primary,
        custom_id="poka:subscriptions:manage:v1",
    )
    async def manage(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.watcher.open_subscription_manager(interaction)

    @discord.ui.button(
        label="查看目前關注",
        emoji="📋",
        style=discord.ButtonStyle.secondary,
        custom_id="poka:subscriptions:list:v1",
    )
    async def show_current(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.watcher.show_current_subscriptions(interaction)


class SubscriptionGroupView(SubscriptionView):
    """Stateless persistent selector so an idle private menu remains usable."""

    def __init__(self, watcher: "MultiBlogWatcher") -> None:
        super().__init__(watcher, timeout=None)

    async def _open_group(
        self, interaction: discord.Interaction, group_key: str
    ) -> None:
        await interaction.response.defer()
        try:
            source = self.watcher.source_for_key(group_key)
            if source is None:
                raise RuntimeError(f"Unknown source {group_key}")
            if interaction.guild_id is None:
                raise RuntimeError("Subscription GUI requires a Discord server")
            catalog = await self.watcher.get_member_catalog(source)
            current_members = _unique_catalog_members(catalog)
            if not current_members:
                raise RuntimeError(f"{source.name} catalog is empty")
            current_member_keys = {member_key for member_key, _ in current_members}
            previous_subscriptions = (
                self.watcher.subscriptions.user_group_subscriptions(
                    interaction.guild_id, interaction.user.id, source.key
                )
            )
            stale_members = tuple(
                (member_key, member_name)
                for member_key, member_name in previous_subscriptions
                if member_key not in current_member_keys
            )
            members = current_members + stale_members
            view = MemberSubscriptionView(
                self.watcher,
                interaction.guild_id,
                interaction.user.id,
                source,
                members,
                page=0,
                stale_keys={member_key for member_key, _ in stale_members},
            )
            await interaction.edit_original_response(
                content=view.render_content(), view=view
            )
        except Exception:
            self.watcher.log.exception("Unable to open subscription GUI for %s", group_key)
            await interaction.edit_original_response(
                content="目前無法讀取官方成員名單，請稍後重新開啟管理畫面。",
                view=None,
            )

    @discord.ui.button(
        label="日向坂46",
        emoji="☀️",
        style=discord.ButtonStyle.primary,
        custom_id="poka:subscriptions:group:hinata:v1",
    )
    async def hinata(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self._open_group(interaction, "hinata")

    @discord.ui.button(
        label="櫻坂46",
        emoji="🌸",
        style=discord.ButtonStyle.primary,
        custom_id="poka:subscriptions:group:sakura:v1",
    )
    async def sakura(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self._open_group(interaction, "sakura")


class MemberSubscriptionSelect(discord.ui.Select):
    def __init__(self, manager: "MemberSubscriptionView") -> None:
        self.manager = manager
        subscribed_keys = manager.subscribed_keys()
        options = [
            discord.SelectOption(
                label=self._option_label(member_key, member_name),
                value=member_key,
                default=member_key in subscribed_keys,
            )
            for member_key, member_name in manager.page_members
        ]
        super().__init__(
            placeholder="勾選要關注的成員（可複選）",
            min_values=0,
            max_values=len(options),
            options=options,
            row=0,
        )

    def _option_label(self, member_key: str, member_name: str) -> str:
        if member_key in self.manager.stale_keys:
            return f"{member_name}（已不在目前官方名單）"[:100]
        if member_name == "ポカ":
            return "ポカ（Poka）"
        return member_name[:100]

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.manager.replace_page(interaction, set(self.values))


class MemberSubscriptionView(SubscriptionView):
    def __init__(
        self,
        watcher: "MultiBlogWatcher",
        guild_id: int | None,
        user_id: int,
        source: SourceConfig,
        members: tuple[tuple[str, str], ...],
        *,
        page: int,
        stale_keys: set[str] | None = None,
    ) -> None:
        super().__init__(watcher, timeout=None)
        if guild_id is None:
            raise ValueError("Subscription GUI requires a Discord server")
        self.guild_id = guild_id
        self.user_id = user_id
        self.source = source
        self.members = members
        self.stale_keys = stale_keys or set()
        self.page_count = max(
            1,
            (len(members) + SUBSCRIPTION_PAGE_SIZE - 1)
            // SUBSCRIPTION_PAGE_SIZE,
        )
        self.page = min(max(page, 0), self.page_count - 1)
        start = self.page * SUBSCRIPTION_PAGE_SIZE
        self.page_members = members[start : start + SUBSCRIPTION_PAGE_SIZE]
        self.add_item(MemberSubscriptionSelect(self))
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= self.page_count - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "這不是你的關注管理畫面，請從公開面板重新開啟。", ephemeral=True
        )
        return False

    def subscribed_keys(self) -> set[str]:
        return {
            member_key
            for member_key, _ in self.watcher.subscriptions.user_group_subscriptions(
                self.guild_id, self.user_id, self.source.key
            )
        }

    def render_content(self, notice: str | None = None) -> str:
        subscribed_count = len(
            self.watcher.subscriptions.user_group_subscriptions(
                self.guild_id, self.user_id, self.source.key
            )
        )
        lines = [
            f"## {self.source.name}關注管理",
            f"第 {self.page + 1}/{self.page_count} 頁 · 目前共關注 {subscribed_count} 位作者",
            "勾選完成後送出即會儲存；取消勾選也會立即取消關注。",
        ]
        if self.stale_keys:
            lines.append(
                "標示為「已不在目前官方名單」的舊關注會保留，取消勾選即可移除。"
            )
        if notice:
            lines.append(notice)
        return "\n".join(lines)

    def refreshed(self, *, page: int | None = None) -> "MemberSubscriptionView":
        return MemberSubscriptionView(
            self.watcher,
            self.guild_id,
            self.user_id,
            self.source,
            self.members,
            page=self.page if page is None else page,
            stale_keys=self.stale_keys,
        )

    async def replace_page(
        self, interaction: discord.Interaction, selected_keys: set[str]
    ) -> None:
        self.watcher.subscriptions.replace_page_subscriptions(
            self.guild_id,
            self.user_id,
            self.source.key,
            self.page_members,
            selected_keys,
        )
        view = self.refreshed()
        await interaction.edit_original_response(
            content=view.render_content("✅ 這一頁的關注設定已儲存。"), view=view
        )

    @discord.ui.button(
        label="上一頁", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1
    )
    async def previous_page(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        view = self.refreshed(page=self.page - 1)
        await interaction.edit_original_response(
            content=view.render_content(), view=view
        )

    @discord.ui.button(
        label="下一頁", emoji="➡️", style=discord.ButtonStyle.secondary, row=1
    )
    async def next_page(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        view = self.refreshed(page=self.page + 1)
        await interaction.edit_original_response(
            content=view.render_content(), view=view
        )

    @discord.ui.button(
        label="清除此頁", emoji="🗑️", style=discord.ButtonStyle.danger, row=1
    )
    async def clear_page(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        await interaction.edit_original_response(
            content=(
                f"確定要取消{self.source.name}第 {self.page + 1} 頁的所有關注嗎？"
                "其他頁與另一團不會受影響。"
            ),
            view=ClearPageConfirmationView(self),
        )

    @discord.ui.button(
        label="返回團體", emoji="↩️", style=discord.ButtonStyle.secondary, row=1
    )
    async def back(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        await interaction.edit_original_response(
            content="請選擇要管理的團體：",
            view=SubscriptionGroupView(self.watcher),
        )


class ClearPageConfirmationView(SubscriptionView):
    def __init__(self, manager: MemberSubscriptionView) -> None:
        super().__init__(manager.watcher, timeout=None)
        self.manager = manager

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.manager.interaction_check(interaction)

    @discord.ui.button(label="確定清除此頁", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        await self.manager.replace_page(interaction, set())

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        view = self.manager.refreshed()
        await interaction.edit_original_response(
            content=view.render_content("已取消清除。"), view=view
        )


TOYOKO_PANEL_CONTENT = (
    "## 🏨 東橫 INN 空房監視器\n"
    "建立個人空房監看後，POKA 會以低頻率依序查詢。發現空房時會優先私訊你；"
    "無法私訊時才會在這個頻道標記你。設定畫面只有操作的人看得到。"
)


class ToyokoView(discord.ui.View):
    def __init__(
        self,
        watcher: "MultiBlogWatcher",
        *,
        timeout: float | None,
        owner_id: int | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.watcher = watcher
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id is None or interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "請從公開面板開啟你自己的東橫監看管理畫面。", ephemeral=True
        )
        return False

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[discord.ui.View],
    ) -> None:
        self.watcher.log.error(
            "Toyoko interaction failed for custom_id=%s user=%s guild=%s",
            getattr(item, "custom_id", None),
            interaction.user.id,
            interaction.guild_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "POKA 處理東橫監看設定時發生錯誤，請從公開面板重新開啟後再試。"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            self.watcher.log.exception("Unable to reply to Toyoko interaction error")


class ToyokoPanelView(ToyokoView):
    def __init__(self, watcher: "MultiBlogWatcher") -> None:
        super().__init__(watcher, timeout=None)

    @discord.ui.button(
        label="新增監看",
        emoji="➕",
        style=discord.ButtonStyle.primary,
        custom_id="toyoko:add-rule:v1",
    )
    async def add_rule(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.watcher.open_toyoko_create(interaction)

    @discord.ui.button(
        label="我的監看",
        emoji="📋",
        style=discord.ButtonStyle.secondary,
        custom_id="toyoko:manage-rules:v1",
    )
    async def manage_rules(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.watcher.open_toyoko_manager(interaction)


class ToyokoCreateMethodView(ToyokoView):
    def __init__(self, watcher: "MultiBlogWatcher", owner_id: int) -> None:
        super().__init__(watcher, timeout=900, owner_id=owner_id)

    @discord.ui.button(
        label="貼上搜尋網址",
        emoji="🔗",
        style=discord.ButtonStyle.primary,
    )
    async def paste_url(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            ToyokoURLModal(self.watcher, interaction.user.id)
        )

    @discord.ui.button(
        label="引導式設定",
        emoji="🗾",
        style=discord.ButtonStyle.secondary,
    )
    async def guided(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        await interaction.edit_original_response(
            content="請先選擇日本地區：",
            view=ToyokoRegionView(self.watcher, interaction.user.id),
        )


class ToyokoModal(discord.ui.Modal):
    def __init__(
        self,
        watcher: "MultiBlogWatcher",
        *,
        title: str,
        owner_id: int,
    ) -> None:
        super().__init__(title=title, timeout=900)
        self.watcher = watcher
        self.owner_id = owner_id

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        self.watcher.log.error(
            "Toyoko modal failed for user=%s guild=%s",
            interaction.user.id,
            interaction.guild_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "POKA 無法儲存這個監看條件，請檢查內容後再試。"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            self.watcher.log.exception("Unable to reply to Toyoko modal error")

    async def save_url(
        self,
        interaction: discord.Interaction,
        search_url: str,
        interval_text: str,
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "請從公開面板重新建立自己的監看。", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            rule, created = await self.watcher.create_toyoko_rule(
                interaction, search_url, interval_text
            )
        except (ValueError, RuntimeError) as error:
            await interaction.edit_original_response(content=f"⚠️ {error}")
            return
        action = "已建立" if created else "已存在"
        await interaction.edit_original_response(
            content=(
                f"✅ 監看規則{action}\n"
                f"{self.watcher.render_toyoko_rule(rule)}\n\n"
                "POKA 已安排檢查；若目前有房，第一次查詢後就會通知你。"
            )
        )


class ToyokoURLModal(ToyokoModal):
    search_url = discord.ui.TextInput(
        label="東橫 INN 搜尋結果網址",
        placeholder="https://www.toyoko-inn.com/china/search/result/?...",
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )
    interval = discord.ui.TextInput(
        label="檢查頻率（分鐘範圍）",
        placeholder="例如 10-15",
        default="10-15",
        max_length=20,
    )

    def __init__(self, watcher: "MultiBlogWatcher", owner_id: int) -> None:
        super().__init__(watcher, title="從搜尋網址建立監看", owner_id=owner_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.save_url(interaction, str(self.search_url), str(self.interval))


class ToyokoRegionSelect(discord.ui.Select):
    def __init__(self, manager: "ToyokoRegionView") -> None:
        self.manager = manager
        super().__init__(
            placeholder="選擇地區",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=region, value=region)
                for region in JAPAN_PREFECTURE_REGIONS
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        region = self.values[0]
        await interaction.edit_original_response(
            content=f"已選擇 **{region}**，請選擇都道府縣：",
            view=ToyokoPrefectureView(
                self.manager.watcher, self.manager.owner_id, region
            ),
        )


class ToyokoRegionView(ToyokoView):
    def __init__(self, watcher: "MultiBlogWatcher", owner_id: int) -> None:
        super().__init__(watcher, timeout=900, owner_id=owner_id)
        self.add_item(ToyokoRegionSelect(self))


class ToyokoPrefectureSelect(discord.ui.Select):
    def __init__(self, manager: "ToyokoPrefectureView") -> None:
        self.manager = manager
        super().__init__(
            placeholder="選擇都道府縣",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=name, value=str(prefecture_id))
                for prefecture_id, name in JAPAN_PREFECTURE_REGIONS[
                    manager.region
                ]
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        prefecture_id = int(self.values[0])
        prefecture_name = next(
            name
            for value, name in JAPAN_PREFECTURE_REGIONS[self.manager.region]
            if value == prefecture_id
        )
        await interaction.edit_original_response(
            content=(
                f"已選擇 **{prefecture_name}**。接著選擇吸菸條件；"
                "點選後會開啟日期、人數與頻率表單。"
            ),
            view=ToyokoSmokingView(
                self.manager.watcher,
                self.manager.owner_id,
                prefecture_id,
                prefecture_name,
            ),
        )


class ToyokoPrefectureView(ToyokoView):
    def __init__(
        self, watcher: "MultiBlogWatcher", owner_id: int, region: str
    ) -> None:
        super().__init__(watcher, timeout=900, owner_id=owner_id)
        self.region = region
        self.add_item(ToyokoPrefectureSelect(self))


class ToyokoSmokingSelect(discord.ui.Select):
    def __init__(self, manager: "ToyokoSmokingView") -> None:
        self.manager = manager
        super().__init__(
            placeholder="選擇禁菸／吸菸條件",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=value)
                for value, label in SMOKING_LABELS.items()
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            ToyokoGuidedModal(
                self.manager.watcher,
                interaction.user.id,
                self.manager.prefecture_id,
                self.manager.prefecture_name,
                self.values[0],
            )
        )


class ToyokoSmokingView(ToyokoView):
    def __init__(
        self,
        watcher: "MultiBlogWatcher",
        owner_id: int,
        prefecture_id: int,
        prefecture_name: str,
    ) -> None:
        super().__init__(watcher, timeout=900, owner_id=owner_id)
        self.prefecture_id = prefecture_id
        self.prefecture_name = prefecture_name
        self.add_item(ToyokoSmokingSelect(self))


class ToyokoGuidedModal(ToyokoModal):
    check_in = discord.ui.TextInput(
        label="入住日期", placeholder="2026-11-21", max_length=10
    )
    check_out = discord.ui.TextInput(
        label="退房日期", placeholder="2026-11-23", max_length=10
    )
    people_and_rooms = discord.ui.TextInput(
        label="人數,房間數", placeholder="例如 1,1", default="1,1", max_length=20
    )
    interval = discord.ui.TextInput(
        label="檢查頻率（分鐘範圍）",
        placeholder="例如 10-15",
        default="10-15",
        max_length=20,
    )

    def __init__(
        self,
        watcher: "MultiBlogWatcher",
        owner_id: int,
        prefecture_id: int,
        prefecture_name: str,
        smoking: str,
    ) -> None:
        super().__init__(
            watcher,
            title=f"設定{prefecture_name}空房監看",
            owner_id=owner_id,
        )
        self.prefecture_id = prefecture_id
        self.smoking = smoking

    async def on_submit(self, interaction: discord.Interaction) -> None:
        parts = re.split(r"[,，、/\s]+", str(self.people_and_rooms).strip())
        if len(parts) != 2:
            await interaction.response.send_message(
                "人數與房間數請填寫成 `1,1`。", ephemeral=True
            )
            return
        try:
            query = build_toyoko_search_url(
                scope_type="prefecture",
                scope_id=self.prefecture_id,
                check_in=str(self.check_in),
                check_out=str(self.check_out),
                people=parts[0],
                rooms=parts[1],
                smoking=self.smoking,
            )
        except ValueError as error:
            await interaction.response.send_message(f"⚠️ {error}", ephemeral=True)
            return
        await self.save_url(interaction, query.search_url, str(self.interval))


class ToyokoRuleSelect(discord.ui.Select):
    def __init__(self, manager: "ToyokoManageView") -> None:
        self.manager = manager
        options: list[discord.SelectOption] = []
        for rule in manager.rules:
            label = f"#{rule.rule_id} {rule.destination}"[:100]
            description = (
                f"{rule.check_in:%Y/%m/%d}–{rule.check_out:%m/%d} · "
                f"{'啟用' if rule.enabled else '暫停'}"
            )[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    description=description,
                    value=str(rule.rule_id),
                    default=rule.rule_id == manager.selected_rule_id,
                )
            )
        super().__init__(
            placeholder="選擇要管理的規則",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        view = self.manager.refreshed(selected_rule_id=int(self.values[0]))
        await interaction.edit_original_response(content=view.render(), view=view)


class ToyokoManageView(ToyokoView):
    def __init__(
        self,
        watcher: "MultiBlogWatcher",
        guild_id: int,
        owner_id: int,
        rules: tuple[ToyokoRule, ...],
        selected_rule_id: int | None = None,
    ) -> None:
        super().__init__(watcher, timeout=900, owner_id=owner_id)
        self.guild_id = guild_id
        self.rules = rules
        rule_ids = {rule.rule_id for rule in rules}
        self.selected_rule_id = (
            selected_rule_id
            if selected_rule_id in rule_ids
            else (rules[0].rule_id if rules else None)
        )
        if rules:
            self.add_item(ToyokoRuleSelect(self))

    @property
    def selected_rule(self) -> ToyokoRule | None:
        return next(
            (
                rule
                for rule in self.rules
                if rule.rule_id == self.selected_rule_id
            ),
            None,
        )

    def render(self, notice: str | None = None) -> str:
        if not self.rules:
            return "你目前沒有東橫 INN 空房監看。"
        selected = self.selected_rule
        lines = ["## 我的東橫空房監看"]
        if notice:
            lines.append(notice)
        if selected is not None:
            lines.extend(("", self.watcher.render_toyoko_rule(selected)))
        return "\n".join(lines)

    def refreshed(
        self, *, selected_rule_id: int | None = None
    ) -> "ToyokoManageView":
        store = self.watcher.require_toyoko_store()
        return ToyokoManageView(
            self.watcher,
            self.guild_id,
            self.owner_id or 0,
            store.user_rules(self.guild_id, self.owner_id or 0),
            selected_rule_id=(
                self.selected_rule_id
                if selected_rule_id is None
                else selected_rule_id
            ),
        )

    @discord.ui.button(label="暫停／恢復", emoji="⏯️", row=1)
    async def toggle(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        rule = self.selected_rule
        if rule is None:
            return
        self.watcher.require_toyoko_store().set_enabled(
            rule.rule_id, interaction.user.id, not rule.enabled
        )
        view = self.refreshed(selected_rule_id=rule.rule_id)
        await interaction.edit_original_response(
            content=view.render("✅ 規則狀態已更新。"), view=view
        )

    @discord.ui.button(label="安排立即檢查", emoji="🔄", row=1)
    async def check_now(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        rule = self.selected_rule
        if rule is None:
            return
        self.watcher.require_toyoko_store().schedule_now(
            rule.rule_id,
            interaction.user.id,
            time.time(),
        )
        view = self.refreshed(selected_rule_id=rule.rule_id)
        await interaction.edit_original_response(
            content=view.render("✅ 已加入查詢佇列，不會跳過全域請求間隔。"),
            view=view,
        )

    @discord.ui.button(
        label="刪除", emoji="🗑️", style=discord.ButtonStyle.danger, row=1
    )
    async def delete(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        rule = self.selected_rule
        if rule is None:
            return
        self.watcher.require_toyoko_store().delete_rule(
            rule.rule_id, interaction.user.id
        )
        view = self.refreshed()
        await interaction.edit_original_response(
            content=view.render("✅ 監看規則已刪除。"),
            view=view if view.rules else None,
        )


class MultiBlogWatcher(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        # Receive MESSAGE_CREATE events from server text channels.  The
        # message_content intent only exposes the text fields; it does not
        # subscribe the client to guild message events by itself.
        intents.guild_messages = True
        intents.message_content = bool(
            config.subscription_channel_id
            and config.enable_text_subscription_commands
        )
        super().__init__(intents=intents)
        self.config = config
        self.states = {
            source.key: StateStore(source.state_db)
            for source in config.sources
            if source.enabled
        }
        self.subscriptions = SubscriptionStore(config.subscription_db)
        sakura_state = self.states.get("sakura")
        self.reactivated_posts = (
            sakura_state.enable_previously_ignored_posts()
            if sakura_state is not None
            else 0
        )
        self.toyoko_store = (
            ToyokoStore(config.toyoko_db) if config.enable_toyoko_watcher else None
        )
        self.toyoko_client: ToyokoClient | None = None
        self.http_session: aiohttp.ClientSession | None = None
        self.workers: dict[str, asyncio.Task[None]] = {}
        self.member_catalogs: dict[str, dict[str, str]] = {}
        self.member_catalog_fetched_at: dict[str, float] = {}
        self.member_catalog_lock = asyncio.Lock()
        self.subscription_panel_lock = asyncio.Lock()
        self.toyoko_panel_lock = asyncio.Lock()
        self.log = logging.getLogger("watcher")

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(
            headers={"User-Agent": self.config.user_agent}
        )
        if self.config.enable_toyoko_watcher:
            self.toyoko_client = ToyokoClient(
                self.http_session,
                self.config.timeout,
                self.config.retries,
                self.config.toyoko_request_gap_seconds,
            )
        if (
            self.config.subscription_channel_id
            and self.config.enable_subscription_gui
        ):
            self.add_view(SubscriptionPanelView(self))
            self.add_view(SubscriptionGroupView(self))
        if self.config.enable_toyoko_watcher and self.config.toyoko_channel_id:
            self.add_view(ToyokoPanelView(self))

    async def on_ready(self) -> None:
        self.log.info("Discord connected as %s", self.user)
        self.log.info(
            "Local image storage is %s",
            "enabled" if self.config.save_images_locally else "disabled",
        )
        if self.reactivated_posts:
            self.log.info(
                "櫻坂46 reactivated %s previously ignored homepage item(s)",
                self.reactivated_posts,
            )
        if self.config.subscription_channel_id:
            if self.config.enable_subscription_gui:
                try:
                    await self.ensure_subscription_panel()
                except Exception:
                    self.log.exception("Unable to create or restore subscription GUI panel")
            self.log.info(
                "Member subscription GUI is %s; text commands are %s in Discord "
                "channel %s",
                "enabled" if self.config.enable_subscription_gui else "disabled",
                (
                    "enabled"
                    if self.config.enable_text_subscription_commands
                    else "disabled"
                ),
                self.config.subscription_channel_id,
            )
        if self.config.enable_toyoko_watcher:
            try:
                await self.ensure_toyoko_panel()
            except Exception:
                self.log.exception("Unable to create or restore Toyoko panel")
            worker = self.workers.get("toyoko")
            if worker is None or worker.done():
                self.workers["toyoko"] = asyncio.create_task(
                    self.watch_toyoko_forever(), name="toyoko-watcher"
                )
            self.log.info(
                "Toyoko watcher enabled in Discord channel %s",
                self.config.toyoko_channel_id,
            )
        for source in self.config.sources:
            if not source.enabled:
                self.log.info("%s watcher disabled by configuration", source.name)
                continue
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

    async def on_message(self, message: discord.Message) -> None:
        if (
            message.author.bot
            or message.guild is None
            or not self.config.enable_text_subscription_commands
        ):
            return
        if (
            not self.config.subscription_channel_id
            or message.channel.id != self.config.subscription_channel_id
        ):
            return
        await self.handle_subscription_message(message)

    @staticmethod
    def source_aliases() -> dict[str, str]:
        return {
            "hinata": "hinata",
            "日向": "hinata",
            "日向坂": "hinata",
            "日向坂46": "hinata",
            "sakura": "sakura",
            "櫻": "sakura",
            "櫻坂": "sakura",
            "櫻坂46": "sakura",
        }

    def source_for_alias(self, value: str) -> SourceConfig | None:
        source_key = self.source_aliases().get(_normalize_member_name(value).lower())
        if source_key is None:
            return None
        return next(
            (source for source in self.config.sources if source.key == source_key),
            None,
        )

    def source_for_key(self, group_key: str) -> SourceConfig | None:
        return next(
            (source for source in self.config.sources if source.key == group_key),
            None,
        )

    def require_toyoko_store(self) -> ToyokoStore:
        if self.toyoko_store is None:
            raise RuntimeError("東橫監視器目前未啟用")
        return self.toyoko_store

    def require_toyoko_client(self) -> ToyokoClient:
        if self.toyoko_client is None:
            raise RuntimeError("東橫監視器的 HTTP client 尚未準備完成")
        return self.toyoko_client

    def _toyoko_panel_setting_key(self) -> str:
        return f"toyoko_panel_message_id:{self.config.toyoko_channel_id}"

    async def pin_toyoko_panel(self, panel_message: discord.Message) -> None:
        if not self.config.pin_toyoko_panel or panel_message.pinned:
            return
        try:
            await panel_message.pin(reason="Keep the Toyoko watch panel available")
        except discord.Forbidden:
            self.log.warning(
                "Unable to pin Toyoko panel; grant Manage Messages in channel %s",
                self.config.toyoko_channel_id,
            )
        except discord.HTTPException:
            self.log.exception("Discord failed to pin Toyoko panel")

    async def ensure_toyoko_panel(self) -> None:
        async with self.toyoko_panel_lock:
            if (
                not self.config.enable_toyoko_watcher
                or not self.config.toyoko_channel_id
            ):
                return
            store = self.require_toyoko_store()
            channel = self.get_channel(self.config.toyoko_channel_id)
            if channel is None:
                channel = await self.fetch_channel(self.config.toyoko_channel_id)
            if not hasattr(channel, "send") or not hasattr(channel, "fetch_message"):
                raise RuntimeError("東橫監視頻道不是可傳送及讀取訊息的頻道")

            setting_key = self._toyoko_panel_setting_key()
            stored_id = store.get_setting(setting_key)
            if stored_id and stored_id.isdigit():
                try:
                    panel_message = await channel.fetch_message(int(stored_id))
                    if self.user is not None and panel_message.author.id == self.user.id:
                        await panel_message.edit(
                            content=TOYOKO_PANEL_CONTENT,
                            view=ToyokoPanelView(self),
                        )
                        await self.pin_toyoko_panel(panel_message)
                        self.log.info(
                            "Toyoko panel restored from message %s", panel_message.id
                        )
                        return
                except discord.NotFound:
                    self.log.info("Stored Toyoko panel was deleted; replacing it")
                store.delete_setting(setting_key)
            elif stored_id:
                store.delete_setting(setting_key)

            panel_message = await channel.send(
                TOYOKO_PANEL_CONTENT,
                view=ToyokoPanelView(self),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            store.set_setting(setting_key, str(panel_message.id))
            await self.pin_toyoko_panel(panel_message)
            self.log.info(
                "Toyoko panel created as message %s in channel %s",
                panel_message.id,
                self.config.toyoko_channel_id,
            )

    async def open_toyoko_create(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "東橫監看只能在 Discord 伺服器內設定。", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "選擇建立方式。精確地區或指定飯店建議使用東橫官方搜尋網址；"
            "引導式設定目前支援日本都道府縣。",
            view=ToyokoCreateMethodView(self, interaction.user.id),
            ephemeral=True,
        )

    async def open_toyoko_manager(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "東橫監看只能在 Discord 伺服器內管理。", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rules = self.require_toyoko_store().user_rules(
            interaction.guild_id, interaction.user.id
        )
        view = ToyokoManageView(
            self,
            interaction.guild_id,
            interaction.user.id,
            rules,
        )
        await interaction.edit_original_response(
            content=view.render(), view=view if rules else None
        )

    async def create_toyoko_rule(
        self,
        interaction: discord.Interaction,
        search_url: str,
        interval_text: str,
    ) -> tuple[ToyokoRule, bool]:
        if interaction.guild_id is None:
            raise ValueError("東橫監看只能在 Discord 伺服器內建立")
        store = self.require_toyoko_store()
        query = parse_toyoko_search_url(search_url)
        existing = next(
            (
                rule
                for rule in store.user_rules(
                    interaction.guild_id, interaction.user.id
                )
                if rule.search_url == query.search_url
            ),
            None,
        )
        if existing is not None:
            return existing, False
        if (
            store.count_user_rules(interaction.guild_id, interaction.user.id)
            >= self.config.toyoko_max_rules_per_user
        ):
            raise ValueError(
                f"每位使用者最多建立 {self.config.toyoko_max_rules_per_user} 條監看"
            )
        interval_min, interval_max = parse_interval_minutes(
            interval_text,
            default_min_seconds=self.config.toyoko_default_min_interval_seconds,
            default_max_seconds=self.config.toyoko_default_max_interval_seconds,
            minimum_seconds=self.config.toyoko_min_allowed_interval_seconds,
        )
        try:
            definition = await self.require_toyoko_client().fetch_definition(query)
        except ToyokoHTTPError as error:
            raise RuntimeError(str(error)) from error
        except Exception as error:
            self.log.exception("Unable to validate Toyoko search URL")
            raise RuntimeError("目前無法讀取這個東橫搜尋結果，請稍後再試") from error
        rule, created = store.add_rule(
            guild_id=interaction.guild_id,
            user_id=interaction.user.id,
            channel_id=self.config.toyoko_channel_id,
            definition=definition,
            interval_min_seconds=interval_min,
            interval_max_seconds=interval_max,
            now=time.time(),
        )
        if created:
            self.log.info(
                "Toyoko rule %s created by user %s for %s",
                rule.rule_id,
                rule.user_id,
                rule.destination,
            )
        return rule, created

    @staticmethod
    def render_toyoko_rule(rule: ToyokoRule) -> str:
        status_labels = {
            "available": "有空房",
            "unavailable": "目前無空房",
            None: "等待第一次檢查",
        }
        interval = (
            f"{rule.interval_min_seconds // 60}–"
            f"{rule.interval_max_seconds // 60} 分鐘"
        )
        return (
            f"**#{rule.rule_id} {rule.destination}** "
            f"({'啟用' if rule.enabled else '暫停'})\n"
            f"{rule.check_in:%Y/%m/%d}–{rule.check_out:%Y/%m/%d} "
            f"({rule.query.nights} 晚) · {rule.people} 人 × {rule.rooms} 間 · "
            f"{SMOKING_LABELS[rule.smoking]}\n"
            f"頻率：{interval} · 狀態："
            f"{status_labels.get(rule.last_status, '查詢異常')}\n"
            f"<{rule.search_url}>"
        )

    async def notify_toyoko_available(
        self, rule: ToyokoRule, result: ToyokoAvailabilityResult
    ) -> None:
        available = sorted(
            result.available_hotels,
            key=lambda hotel: (
                hotel.lowest_price if hotel.lowest_price is not None else 10**12,
                hotel.name,
            ),
        )
        embed = discord.Embed(
            title="🏨 東橫 INN 發現空房",
            description=(
                f"**{rule.destination}**\n"
                f"{rule.check_in:%Y/%m/%d}–{rule.check_out:%Y/%m/%d} "
                f"({rule.query.nights} 晚) · {rule.people} 人 × {rule.rooms} 間 · "
                f"{SMOKING_LABELS[rule.smoking]}\n\n"
                "空房與價格可能隨時變動，請進入官方網站再次確認。"
            ),
            url=rule.search_url,
            color=0x2E8B57,
        )
        for hotel in available[:20]:
            price = (
                f"¥ {hotel.lowest_price:,} 起"
                if hotel.lowest_price is not None
                else "有空房"
            )
            embed.add_field(name=hotel.name[:256], value=price, inline=False)
        if len(available) > 20:
            embed.set_footer(text=f"另有 {len(available) - 20} 間，請查看官方頁面")

        try:
            user = self.get_user(rule.user_id)
            if user is None:
                user = await self.fetch_user(rule.user_id)
            await user.send(embed=embed)
            self.log.info(
                "Toyoko availability for rule %s sent by DM to user %s",
                rule.rule_id,
                rule.user_id,
            )
            return
        except (discord.Forbidden, discord.HTTPException):
            self.log.info(
                "Toyoko DM failed for user %s; falling back to channel %s",
                rule.user_id,
                rule.channel_id,
            )

        channel = self.get_channel(rule.channel_id)
        if channel is None:
            channel = await self.fetch_channel(rule.channel_id)
        if not hasattr(channel, "send"):
            raise RuntimeError("東橫監視頻道不是可傳送訊息的頻道")
        await channel.send(
            content=f"<@{rule.user_id}> 東橫 INN 發現空房：",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                users=[discord.Object(id=rule.user_id)],
                roles=False,
                replied_user=False,
            ),
        )

    async def watch_toyoko_forever(self) -> None:
        store = self.require_toyoko_store()
        client = self.require_toyoko_client()
        while not self.is_closed():
            try:
                expired = store.disable_expired(japan_today())
                if expired:
                    self.log.info("Toyoko watcher disabled %s expired rule(s)", expired)
                due_rules = store.due_rules(time.time())
                grouped: dict[str, list[ToyokoRule]] = {}
                for rule in due_rules:
                    grouped.setdefault(rule.search_url, []).append(rule)
                for group_number, rules in enumerate(grouped.values()):
                    representative = rules[0]
                    try:
                        result = await client.fetch_availability(
                            representative.query, representative.hotels
                        )
                        if result.status == "unknown":
                            raise RuntimeError("東橫空房資料不完整")
                        now = time.time()
                        for rule in rules:
                            try:
                                if should_notify_availability(
                                    rule.last_status, rule.last_available, result
                                ):
                                    await self.notify_toyoko_available(rule, result)
                                store.record_success(rule, result, now=now)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                store.record_failure(rule, now=now)
                                self.log.exception(
                                    "Toyoko notification failed for rule %s; "
                                    "will retry",
                                    rule.rule_id,
                                )
                        self.log.info(
                            "Toyoko poll %s returned %s for %s rule(s)",
                            representative.destination,
                            result.status,
                            len(rules),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        retry_after = (
                            error.retry_after
                            if isinstance(error, ToyokoHTTPError)
                            else None
                        )
                        now = time.time()
                        for rule in rules:
                            store.record_failure(
                                rule, now=now, retry_after=retry_after
                            )
                        self.log.exception(
                            "Toyoko poll failed for %s; backing off",
                            representative.destination,
                        )
                    if group_number < len(grouped) - 1:
                        await asyncio.sleep(self.config.toyoko_rule_gap_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("Toyoko scheduler failed; retrying")
            await asyncio.sleep(5)

    def _subscription_panel_setting_key(self) -> str:
        return f"subscription_panel_message_id:{self.config.subscription_channel_id}"

    async def pin_subscription_panel(self, panel_message: discord.Message) -> None:
        if not self.config.pin_subscription_panel or panel_message.pinned:
            return
        try:
            await panel_message.pin(reason="Keep the blog subscription panel easy to find")
            self.log.info("Subscription GUI panel %s pinned", panel_message.id)
        except discord.Forbidden:
            self.log.warning(
                "Unable to pin subscription GUI panel %s; grant Manage Messages "
                "in subscription channel %s",
                panel_message.id,
                self.config.subscription_channel_id,
            )
        except discord.HTTPException:
            self.log.exception(
                "Discord failed to pin subscription GUI panel %s", panel_message.id
            )

    async def ensure_subscription_panel(self) -> None:
        """Restore the single public panel, or create it if it was deleted."""
        async with self.subscription_panel_lock:
            channel_id = self.config.subscription_channel_id
            if not channel_id or not self.config.enable_subscription_gui:
                return
            channel = self.get_channel(channel_id)
            if channel is None:
                channel = await self.fetch_channel(channel_id)
            if not hasattr(channel, "send") or not hasattr(channel, "fetch_message"):
                raise RuntimeError(
                    f"訂閱頻道 ID {channel_id} 不是可傳送及讀取訊息的頻道"
                )

            setting_key = self._subscription_panel_setting_key()
            stored_message_id = self.subscriptions.get_setting(setting_key)
            if stored_message_id and stored_message_id.isdigit():
                try:
                    panel_message = await channel.fetch_message(int(stored_message_id))
                    if self.user is not None and panel_message.author.id == self.user.id:
                        await panel_message.edit(
                            content=SUBSCRIPTION_PANEL_CONTENT,
                            view=SubscriptionPanelView(self),
                        )
                        await self.pin_subscription_panel(panel_message)
                        self.log.info(
                            "Subscription GUI panel restored from message %s",
                            panel_message.id,
                        )
                        return
                    self.log.warning(
                        "Stored subscription panel message %s is not owned by this bot; "
                        "creating a replacement",
                        stored_message_id,
                    )
                except discord.NotFound:
                    self.log.info(
                        "Stored subscription panel message %s was deleted; creating a "
                        "replacement",
                        stored_message_id,
                    )
                self.subscriptions.delete_setting(setting_key)
            elif stored_message_id:
                self.subscriptions.delete_setting(setting_key)

            panel_message = await channel.send(
                SUBSCRIPTION_PANEL_CONTENT,
                view=SubscriptionPanelView(self),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.subscriptions.set_setting(setting_key, str(panel_message.id))
            await self.pin_subscription_panel(panel_message)
            self.log.info(
                "Subscription GUI panel created as message %s in channel %s",
                panel_message.id,
                channel_id,
            )

    async def open_subscription_manager(
        self, interaction: discord.Interaction
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "關注功能只能在 Discord 伺服器內使用。", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "請選擇要管理的團體：",
            view=SubscriptionGroupView(self),
            ephemeral=True,
        )

    async def show_current_subscriptions(
        self, interaction: discord.Interaction
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "關注功能只能在 Discord 伺服器內使用。", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = self.subscriptions.user_subscriptions(
            interaction.guild_id, interaction.user.id
        )
        if not rows:
            content = "你目前沒有關注任何成員。"
        else:
            group_names = {source.key: source.name for source in self.config.sources}
            content = "## 你目前的關注清單\n" + "\n".join(
                f"- {group_names.get(group_key, group_key)}：{member_name}"
                for group_key, member_name in rows
            )
        await interaction.edit_original_response(content=content)

    async def get_member_catalog(
        self, source: SourceConfig, *, force: bool = False
    ) -> dict[str, str]:
        now = asyncio.get_running_loop().time()
        cached = self.member_catalogs.get(source.key)
        fetched_at = self.member_catalog_fetched_at.get(source.key, 0.0)
        if (
            cached is not None
            and not force
            and now - fetched_at < self.config.member_catalog_refresh_seconds
        ):
            return cached

        async with self.member_catalog_lock:
            now = asyncio.get_running_loop().time()
            cached = self.member_catalogs.get(source.key)
            fetched_at = self.member_catalog_fetched_at.get(source.key, 0.0)
            if (
                cached is not None
                and not force
                and now - fetched_at < self.config.member_catalog_refresh_seconds
            ):
                return cached
            if self.http_session is None:
                raise RuntimeError("HTTP session is not ready")

            fetcher = BlogIndexFetcher(
                self.http_session,
                source.member_index_url,
                source.name,
                self.config.timeout,
                self.config.retries,
            )
            html = await fetcher.fetch()
            names = parse_member_names(html, source.key)
            if not names:
                raise RuntimeError(
                    f"{source.name} official member page contained no members"
                )
            catalog = subscription_author_catalog(source.key, names)
            self.member_catalogs[source.key] = catalog
            self.member_catalog_fetched_at[source.key] = now
            self.log.info(
                "%s member catalog refreshed with %s member(s)",
                source.name,
                len(set(catalog.values())),
            )
            return catalog

    async def handle_subscription_message(self, message: discord.Message) -> None:
        parts = message.content.strip().split(maxsplit=2)
        if not parts:
            return
        command = parts[0].lower()
        subscribe_commands = {"!關注", "!訂閱", "!subscribe", "!sub"}
        unsubscribe_commands = {
            "!取消關注",
            "!取消訂閱",
            "!取消",
            "!unsubscribe",
            "!unsub",
        }
        list_commands = {"!我的關注", "!關注清單", "!subscriptions", "!subs"}
        help_commands = {"!關注說明", "!關注幫助", "!help", "!subscribe-help"}

        if command in help_commands:
            await message.reply(
                "可用指令：\n"
                "`!關注 <日向坂46|櫻坂46> <成員名字>[、<成員名字>...]`\n"
                "`!取消關注 <日向坂46|櫻坂46> <成員名字>[、<成員名字>...]`\n"
                "`!我的關注`\n"
                "多位成員請用 `、`、逗號、分號或換行分隔。\n"
                "日向坂的 `ポカ` 也可輸入 `ぽか` 或 `Poka`。",
                mention_author=False,
            )
            return
        if command in list_commands:
            rows = self.subscriptions.user_subscriptions(
                message.guild.id, message.author.id
            )
            if not rows:
                await message.reply("你目前沒有關注任何成員。", mention_author=False)
                return
            group_names = {source.key: source.name for source in self.config.sources}
            content = "\n".join(
                f"- {group_names.get(group_key, group_key)}：{member_name}"
                for group_key, member_name in rows
            )
            await message.reply(
                f"你目前的關注清單：\n{content}", mention_author=False
            )
            return
        if command not in subscribe_commands and command not in unsubscribe_commands:
            return

        if len(parts) != 3:
            await message.reply(
                "格式錯誤，請使用："
                "`!關注 <日向坂46|櫻坂46> <成員名字>[、<成員名字>...]`",
                mention_author=False,
            )
            return

        source = self.source_for_alias(parts[1])
        if source is None:
            await message.reply(
                "找不到團體，請使用 `日向坂46` 或 `櫻坂46`。",
                mention_author=False,
            )
            return

        try:
            catalog = await self.get_member_catalog(source)
        except Exception:
            self.log.exception("Unable to refresh %s member catalog", source.name)
            await message.reply(
                "目前無法讀取官方成員名單，請稍後再試。",
                mention_author=False,
            )
            return

        requested_names = _split_member_names(parts[2])
        if not requested_names:
            await message.reply(
                "請至少輸入一位成員；多位成員請用 `、`、逗號、分號或換行分隔。",
                mention_author=False,
            )
            return

        changed_names: list[str] = []
        unchanged_names: list[str] = []
        unknown_names: list[str] = []
        processed_member_keys: set[str] = set()
        for requested_name in requested_names:
            requested_key = _member_lookup_key(requested_name)
            member_name = catalog.get(requested_key)
            if member_name is None:
                unknown_names.append(requested_name)
                continue
            member_key = _member_lookup_key(member_name)
            if member_key in processed_member_keys:
                continue
            processed_member_keys.add(member_key)

            if command in subscribe_commands:
                changed = self.subscriptions.subscribe(
                    message.guild.id,
                    message.author.id,
                    source.key,
                    member_key,
                    member_name,
                )
            else:
                changed = self.subscriptions.unsubscribe(
                    message.guild.id,
                    message.author.id,
                    source.key,
                    member_key,
                )
            (changed_names if changed else unchanged_names).append(member_name)

        response_lines = [f"{source.name}關注處理完成："]
        if command in subscribe_commands:
            if changed_names:
                response_lines.append(f"✅ 已成功關注：{'、'.join(changed_names)}")
            if unchanged_names:
                response_lines.append(
                    f"ℹ️ 已經關注，不會重複建立：{'、'.join(unchanged_names)}"
                )
        else:
            if changed_names:
                response_lines.append(f"✅ 已取消關注：{'、'.join(changed_names)}")
            if unchanged_names:
                response_lines.append(
                    f"ℹ️ 原本沒有關注：{'、'.join(unchanged_names)}"
                )
        if unknown_names:
            response_lines.append(
                f"⚠️ 找不到{source.name}可訂閱作者：{'、'.join(unknown_names)}"
            )
        await message.reply("\n".join(response_lines), mention_author=False)

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
        mention_id_set = set(member_mention_ids(source, post.author))
        guild = getattr(channel, "guild", None)
        if guild is not None:
            mention_id_set.update(
                self.subscriptions.subscriber_ids(
                    guild.id,
                    source.key,
                    _member_lookup_key(post.author),
                )
            )
        mention_ids = tuple(sorted(mention_id_set))
        mention_line = " ".join(f"<@{user_id}>" for user_id in mention_ids)
        heading = f"🆕 **[{source.name}] {post.title}**"
        details = " · ".join(part for part in (post.author, post.published_at) if part)
        heading_content = "\n".join(
            part for part in (mention_line, heading, details, f"<{post.url}>") if part
        )
        heading_mentions = discord.AllowedMentions(
            everyone=False,
            users=[discord.Object(id=user_id) for user_id in mention_ids],
            roles=False,
            replied_user=False,
        )
        missing_images = [
            image_url
            for image_url in post.image_urls
            if not state.image_sent(post.post_id, image_url)
        ]

        if not missing_images:
            if needs_heading:
                await channel.send(
                    heading_content,
                    allowed_mentions=heading_mentions,
                )
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
            await channel.send(
                content=content,
                embeds=embeds,
                allowed_mentions=(
                    heading_mentions
                    if content is not None
                    else discord.AllowedMentions.none()
                ),
            )
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
        downloader = (
            BlogImageDownloader(
                self.http_session,
                source.image_dir,
                self.config.timeout,
                self.config.retries,
            )
            if self.config.save_images_locally
            else None
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
                        if downloader is not None:
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
        self.subscriptions.close()
        if self.toyoko_store is not None:
            self.toyoko_store.close()
        await super().close()


# Keep the original class name import-compatible for existing tests/users.
HinataWatcher = MultiBlogWatcher


def main() -> int:
    env_file = os.getenv("POKA_ENV_FILE", ".env")
    load_dotenv(ROOT / env_file)
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
