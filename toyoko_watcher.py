from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import aiohttp
from bs4 import BeautifulSoup


TOYOKO_HOST = "www.toyoko-inn.com"
TOYOKO_SEARCH_PATH = "/china/search/result/"
TOYOKO_PRICE_ENDPOINT = "https://www.toyoko-inn.com/api/trpc/hotels.availabilities.prices"
TOYOKO_PRICE_CHUNK_SIZE = 10
JAPAN_TIMEZONE = timezone(timedelta(hours=9))

AVAILABLE = "available"
UNAVAILABLE = "unavailable"
UNKNOWN = "unknown"

SMOKING_LABELS = {
    "noSmoking": "禁菸",
    "smoking": "吸菸",
    "all": "不限",
}

JAPAN_PREFECTURE_REGIONS: dict[str, tuple[tuple[int, str], ...]] = {
    "北海道": ((1, "北海道"),),
    "東北": (
        (2, "青森縣"),
        (3, "岩手縣"),
        (4, "宮城縣"),
        (5, "秋田縣"),
        (6, "山形縣"),
        (7, "福島縣"),
    ),
    "關東": (
        (8, "茨城縣"),
        (9, "栃木縣"),
        (10, "群馬縣"),
        (11, "埼玉縣"),
        (12, "千葉縣"),
        (13, "東京都"),
        (14, "神奈川縣"),
    ),
    "中部": (
        (15, "新潟縣"),
        (16, "富山縣"),
        (17, "石川縣"),
        (18, "福井縣"),
        (19, "山梨縣"),
        (20, "長野縣"),
        (21, "岐阜縣"),
        (22, "靜岡縣"),
        (23, "愛知縣"),
    ),
    "近畿": (
        (24, "三重縣"),
        (25, "滋賀縣"),
        (26, "京都府"),
        (27, "大阪府"),
        (28, "兵庫縣"),
        (29, "奈良縣"),
        (30, "和歌山縣"),
    ),
    "中國": (
        (31, "鳥取縣"),
        (32, "島根縣"),
        (33, "岡山縣"),
        (34, "廣島縣"),
        (35, "山口縣"),
    ),
    "四國": (
        (36, "德島縣"),
        (37, "香川縣"),
        (38, "愛媛縣"),
        (39, "高知縣"),
    ),
    "九州・沖繩": (
        (40, "福岡縣"),
        (41, "佐賀縣"),
        (42, "長崎縣"),
        (43, "熊本縣"),
        (44, "大分縣"),
        (45, "宮崎縣"),
        (46, "鹿兒島縣"),
        (47, "沖繩縣"),
    ),
}
JAPAN_PREFECTURE_NAMES = {
    prefecture_id: name
    for prefectures in JAPAN_PREFECTURE_REGIONS.values()
    for prefecture_id, name in prefectures
}


class ToyokoError(RuntimeError):
    pass


class ToyokoHTTPError(ToyokoError):
    def __init__(self, status: int, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class ToyokoQuery:
    search_url: str
    scope_type: str
    scope_id: str
    check_in: date
    check_out: date
    people: int
    rooms: int
    smoking: str

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days


@dataclass(frozen=True)
class ToyokoHotelTarget:
    hotel_code: str
    name: str


@dataclass(frozen=True)
class ToyokoSearchDefinition:
    query: ToyokoQuery
    destination: str
    hotels: tuple[ToyokoHotelTarget, ...]


@dataclass(frozen=True)
class ToyokoHotelAvailability:
    hotel_code: str
    name: str
    available: bool | None
    lowest_price: int | None
    under_maintenance: bool = False


@dataclass(frozen=True)
class ToyokoAvailabilityResult:
    status: str
    hotels: tuple[ToyokoHotelAvailability, ...]

    @property
    def available_hotels(self) -> tuple[ToyokoHotelAvailability, ...]:
        return tuple(hotel for hotel in self.hotels if hotel.available)

    @property
    def snapshot(self) -> dict[str, int]:
        return {
            hotel.hotel_code: hotel.lowest_price or 0
            for hotel in self.available_hotels
        }

    @property
    def result_hash(self) -> str:
        payload = [
            (
                hotel.hotel_code,
                hotel.available,
                hotel.lowest_price,
                hotel.under_maintenance,
            )
            for hotel in self.hotels
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class ToyokoRule:
    rule_id: int
    guild_id: int
    user_id: int
    channel_id: int
    search_url: str
    destination: str
    scope_type: str
    scope_id: str
    check_in: date
    check_out: date
    people: int
    rooms: int
    smoking: str
    interval_min_seconds: int
    interval_max_seconds: int
    hotels: tuple[ToyokoHotelTarget, ...]
    enabled: bool
    next_check_at: float
    last_status: str | None
    last_available: dict[str, int]
    consecutive_errors: int

    @property
    def query(self) -> ToyokoQuery:
        return ToyokoQuery(
            search_url=self.search_url,
            scope_type=self.scope_type,
            scope_id=self.scope_id,
            check_in=self.check_in,
            check_out=self.check_out,
            people=self.people,
            rooms=self.rooms,
            smoking=self.smoking,
        )


def japan_today() -> date:
    return datetime.now(JAPAN_TIMEZONE).date()


def _single_query_value(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name, [])
    if len(values) != 1 or not values[0].strip():
        raise ValueError(f"東橫搜尋網址缺少有效的 {name} 參數")
    return values[0].strip()


def _bounded_int(value: str, label: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{label}必須是整數") from error
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{label}必須介於 {minimum} 到 {maximum}")
    return parsed


def _parse_date(value: str, label: str) -> date:
    normalized = value.strip().replace("/", "-")
    try:
        return date.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{label}必須使用 YYYY-MM-DD 或 YYYY/MM/DD") from error


def parse_toyoko_search_url(
    value: str, *, today: date | None = None
) -> ToyokoQuery:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.hostname != TOYOKO_HOST:
        raise ValueError("只能使用 https://www.toyoko-inn.com 的官方搜尋網址")
    if parsed.path.rstrip("/") != TOYOKO_SEARCH_PATH.rstrip("/"):
        raise ValueError("請貼上東橫 INN 的搜尋結果網址")

    query = parse_qs(parsed.query, keep_blank_values=True)
    scope_values = [
        (name, query[name][0].strip())
        for name in ("hotel", "area", "prefecture")
        if len(query.get(name, [])) == 1 and query[name][0].strip()
    ]
    if len(scope_values) != 1:
        raise ValueError("搜尋網址必須指定一個飯店、地區或都道府縣")
    scope_type, scope_id = scope_values[0]
    if not scope_id.isdigit():
        raise ValueError("搜尋範圍代碼格式不正確")

    check_in = _parse_date(_single_query_value(query, "start"), "入住日期")
    check_out = _parse_date(_single_query_value(query, "end"), "退房日期")
    if check_out <= check_in:
        raise ValueError("退房日期必須晚於入住日期")
    if (check_out - check_in).days > 30:
        raise ValueError("一次監看最多設定 30 晚")
    if check_in < (today or japan_today()):
        raise ValueError("入住日期不能早於今天")

    people = _bounded_int(
        _single_query_value(query, "people"), "人數", 1, 10
    )
    rooms = _bounded_int(_single_query_value(query, "room"), "房間數", 1, 10)
    if rooms > people:
        raise ValueError("房間數不能大於住宿人數")
    smoking = _single_query_value(query, "smoking")
    if smoking not in SMOKING_LABELS:
        raise ValueError("吸菸條件必須是禁菸、吸菸或不限")

    canonical_parameters = {
        scope_type: scope_id,
        "people": str(people),
        "room": str(rooms),
        "smoking": smoking,
        "start": check_in.isoformat(),
        "end": check_out.isoformat(),
    }
    canonical_url = urlunparse(
        ("https", TOYOKO_HOST, TOYOKO_SEARCH_PATH, "", urlencode(canonical_parameters), "")
    )
    return ToyokoQuery(
        search_url=canonical_url,
        scope_type=scope_type,
        scope_id=scope_id,
        check_in=check_in,
        check_out=check_out,
        people=people,
        rooms=rooms,
        smoking=smoking,
    )


def build_toyoko_search_url(
    *,
    scope_type: str,
    scope_id: str | int,
    check_in: str,
    check_out: str,
    people: str | int,
    rooms: str | int,
    smoking: str,
    today: date | None = None,
) -> ToyokoQuery:
    if scope_type not in {"hotel", "area", "prefecture"}:
        raise ValueError("不支援的東橫搜尋範圍")
    parameters = {
        scope_type: str(scope_id),
        "people": str(people),
        "room": str(rooms),
        "smoking": smoking,
        "start": check_in,
        "end": check_out,
    }
    url = urlunparse(
        ("https", TOYOKO_HOST, TOYOKO_SEARCH_PATH, "", urlencode(parameters), "")
    )
    return parse_toyoko_search_url(url, today=today)


def parse_interval_minutes(
    value: str,
    *,
    default_min_seconds: int,
    default_max_seconds: int,
    minimum_seconds: int = 600,
) -> tuple[int, int]:
    normalized = value.strip()
    if not normalized:
        minimum = default_min_seconds
        maximum = default_max_seconds
    else:
        match = re.fullmatch(r"(\d+)\s*(?:[-~～—]\s*(\d+))?", normalized)
        if match is None:
            raise ValueError("檢查頻率請填寫例如 10-15")
        minimum = int(match.group(1)) * 60
        maximum = int(match.group(2) or match.group(1)) * 60
    if minimum > maximum:
        raise ValueError("檢查頻率的起始分鐘不能大於結束分鐘")
    if minimum < minimum_seconds:
        raise ValueError(f"檢查頻率不得低於 {minimum_seconds // 60} 分鐘")
    if maximum > 24 * 60 * 60:
        raise ValueError("檢查頻率不得超過 24 小時")
    return minimum, maximum


def parse_toyoko_search_page(
    html: str, query: ToyokoQuery
) -> ToyokoSearchDefinition:
    soup = BeautifulSoup(html, "html.parser")
    next_data = soup.select_one("#__NEXT_DATA__")
    if next_data is None or not next_data.get_text(strip=True):
        raise ToyokoError("東橫搜尋頁缺少結構化資料")
    try:
        payload = json.loads(next_data.get_text())
        search_response = payload["props"]["pageProps"]["searchResponse"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ToyokoError("無法解析東橫搜尋頁資料") from error

    raw_hotels = search_response.get("hotels")
    if not isinstance(raw_hotels, list) or not raw_hotels:
        raise ToyokoError("東橫搜尋頁沒有可辨識的飯店")
    hotels: list[ToyokoHotelTarget] = []
    seen_codes: set[str] = set()
    for hotel in raw_hotels:
        if not isinstance(hotel, dict):
            continue
        code = str(hotel.get("hotelCode", "")).strip()
        name = str(hotel.get("name", "")).strip()
        if not code or not name or code in seen_codes:
            continue
        seen_codes.add(code)
        hotels.append(ToyokoHotelTarget(code, name))
    if not hotels:
        raise ToyokoError("東橫搜尋頁沒有有效的飯店代碼")

    destination = ""
    for key in ("area", "prefecture", "spot"):
        item = search_response.get(key)
        if isinstance(item, dict) and str(item.get("name", "")).strip():
            destination = str(item["name"]).strip()
            break
        if key == "prefecture" and isinstance(item, dict):
            try:
                destination = JAPAN_PREFECTURE_NAMES[int(item.get("id"))]
                break
            except (KeyError, TypeError, ValueError):
                pass
    if not destination and query.scope_type == "hotel":
        destination = hotels[0].name
    if not destination:
        destination = f"{query.scope_type} {query.scope_id}"
    return ToyokoSearchDefinition(query, destination, tuple(hotels))


def parse_toyoko_price_response(
    payload: Any, hotels: tuple[ToyokoHotelTarget, ...]
) -> ToyokoAvailabilityResult:
    try:
        prices = payload[0]["result"]["data"]["json"]["prices"]
    except (IndexError, KeyError, TypeError) as error:
        raise ToyokoError("東橫空房價格回應格式不正確") from error
    if not isinstance(prices, dict):
        raise ToyokoError("東橫空房價格資料不是 object")

    results: list[ToyokoHotelAvailability] = []
    explicit_count = 0
    for hotel in hotels:
        raw = prices.get(hotel.hotel_code)
        if not isinstance(raw, dict):
            results.append(
                ToyokoHotelAvailability(hotel.hotel_code, hotel.name, None, None)
            )
            continue
        available_value = raw.get("existEnoughVacantRooms")
        available = available_value if isinstance(available_value, bool) else None
        if available is not None:
            explicit_count += 1
        raw_price = raw.get("lowestPrice")
        lowest_price = (
            int(raw_price)
            if available and isinstance(raw_price, (int, float)) and raw_price > 0
            else None
        )
        results.append(
            ToyokoHotelAvailability(
                hotel.hotel_code,
                hotel.name,
                available,
                lowest_price,
                bool(raw.get("isUnderMaintenance", False)),
            )
        )

    if any(hotel.available for hotel in results):
        status = AVAILABLE
    elif explicit_count == len(hotels) and all(
        hotel.available is False for hotel in results
    ):
        status = UNAVAILABLE
    else:
        status = UNKNOWN
    return ToyokoAvailabilityResult(status, tuple(results))


def should_notify_availability(
    previous_status: str | None,
    previous_available: dict[str, int],
    result: ToyokoAvailabilityResult,
) -> bool:
    if result.status != AVAILABLE:
        return False
    if previous_status != AVAILABLE:
        return True
    current = result.snapshot
    return any(
        code not in previous_available or price < previous_available[code]
        for code, price in current.items()
    )


def next_interval(minimum_seconds: int, maximum_seconds: int) -> int:
    return random.randint(minimum_seconds, maximum_seconds)


def error_backoff_seconds(consecutive_errors: int) -> int:
    return min(6 * 60 * 60, 30 * 60 * (2 ** max(0, consecutive_errors - 1)))


class ToyokoClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        timeout_seconds: int,
        retries: int,
        request_gap_seconds: int,
    ) -> None:
        self.session = session
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.retries = retries
        self.request_gap_seconds = request_gap_seconds
        self.log = logging.getLogger("toyoko")

    async def _get_text(self, url: str, **kwargs: Any) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                async with self.session.get(
                    url, timeout=self.timeout, **kwargs
                ) as response:
                    if response.status == 429:
                        retry_after = response.headers.get("Retry-After", "")
                        raise ToyokoHTTPError(
                            429,
                            "東橫網站要求降低查詢頻率",
                            int(retry_after) if retry_after.isdigit() else None,
                        )
                    if response.status == 403:
                        raise ToyokoHTTPError(403, "東橫網站拒絕這次查詢")
                    if response.status >= 500:
                        raise ToyokoHTTPError(
                            response.status, f"東橫網站回傳 HTTP {response.status}"
                        )
                    if response.status != 200:
                        raise ToyokoHTTPError(
                            response.status, f"東橫網站回傳 HTTP {response.status}"
                        )
                    return await response.text()
            except (aiohttp.ClientError, asyncio.TimeoutError, ToyokoHTTPError) as error:
                last_error = error
                if isinstance(error, ToyokoHTTPError) and error.status in {403, 429}:
                    raise
                if attempt < self.retries:
                    await asyncio.sleep(min(2 ** (attempt - 1), 5))
        raise ToyokoError("讀取東橫網站失敗") from last_error

    async def fetch_definition(self, query: ToyokoQuery) -> ToyokoSearchDefinition:
        html = await self._get_text(query.search_url)
        return parse_toyoko_search_page(html, query)

    async def fetch_availability(
        self,
        query: ToyokoQuery,
        hotels: tuple[ToyokoHotelTarget, ...],
    ) -> ToyokoAvailabilityResult:
        combined: list[ToyokoHotelAvailability] = []
        for offset in range(0, len(hotels), TOYOKO_PRICE_CHUNK_SIZE):
            chunk = hotels[offset : offset + TOYOKO_PRICE_CHUNK_SIZE]
            if offset:
                await asyncio.sleep(self.request_gap_seconds)
            input_payload = {
                "0": {
                    "json": {
                        "hotelCodes": [hotel.hotel_code for hotel in chunk],
                        "checkinDate": f"{query.check_in.isoformat()}T00:00:00.000Z",
                        "checkoutDate": f"{query.check_out.isoformat()}T00:00:00.000Z",
                        "numberOfPeople": query.people,
                        "numberOfRoom": query.rooms,
                        "smokingType": query.smoking,
                    },
                    "meta": {
                        "values": {
                            "checkinDate": ["Date"],
                            "checkoutDate": ["Date"],
                        }
                    },
                }
            }
            text = await self._get_text(
                TOYOKO_PRICE_ENDPOINT,
                params={
                    "batch": "1",
                    "input": json.dumps(
                        input_payload, ensure_ascii=False, separators=(",", ":")
                    ),
                },
                headers={"Accept": "application/json"},
            )
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as error:
                raise ToyokoError("東橫空房價格回應不是 JSON") from error
            combined.extend(parse_toyoko_price_response(payload, chunk).hotels)

        result = ToyokoAvailabilityResult(UNKNOWN, tuple(combined))
        if any(hotel.available for hotel in combined):
            status = AVAILABLE
        elif len(combined) == len(hotels) and all(
            hotel.available is False for hotel in combined
        ):
            status = UNAVAILABLE
        else:
            status = UNKNOWN
        return ToyokoAvailabilityResult(status, result.hotels)


class ToyokoStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS toyoko_watch_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                search_url TEXT NOT NULL,
                destination TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                check_in TEXT NOT NULL,
                check_out TEXT NOT NULL,
                people INTEGER NOT NULL,
                rooms INTEGER NOT NULL,
                smoking TEXT NOT NULL,
                interval_min_seconds INTEGER NOT NULL,
                interval_max_seconds INTEGER NOT NULL,
                hotels_json TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                next_check_at REAL NOT NULL,
                last_status TEXT,
                last_result_hash TEXT,
                last_available_json TEXT NOT NULL DEFAULT '{}',
                last_checked_at TEXT,
                consecutive_errors INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, user_id, search_url)
            );
            CREATE INDEX IF NOT EXISTS toyoko_due_rules
                ON toyoko_watch_rules(enabled, next_check_at);
            CREATE TABLE IF NOT EXISTS toyoko_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    @staticmethod
    def _hotels_json(hotels: tuple[ToyokoHotelTarget, ...]) -> str:
        return json.dumps(
            [
                {"hotel_code": hotel.hotel_code, "name": hotel.name}
                for hotel in hotels
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _row_to_rule(row: sqlite3.Row) -> ToyokoRule:
        raw_hotels = json.loads(str(row["hotels_json"]))
        hotels = tuple(
            ToyokoHotelTarget(str(hotel["hotel_code"]), str(hotel["name"]))
            for hotel in raw_hotels
        )
        raw_available = json.loads(str(row["last_available_json"] or "{}"))
        return ToyokoRule(
            rule_id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            channel_id=int(row["channel_id"]),
            search_url=str(row["search_url"]),
            destination=str(row["destination"]),
            scope_type=str(row["scope_type"]),
            scope_id=str(row["scope_id"]),
            check_in=date.fromisoformat(str(row["check_in"])),
            check_out=date.fromisoformat(str(row["check_out"])),
            people=int(row["people"]),
            rooms=int(row["rooms"]),
            smoking=str(row["smoking"]),
            interval_min_seconds=int(row["interval_min_seconds"]),
            interval_max_seconds=int(row["interval_max_seconds"]),
            hotels=hotels,
            enabled=bool(row["enabled"]),
            next_check_at=float(row["next_check_at"]),
            last_status=str(row["last_status"]) if row["last_status"] else None,
            last_available={str(key): int(value) for key, value in raw_available.items()},
            consecutive_errors=int(row["consecutive_errors"]),
        )

    def add_rule(
        self,
        *,
        guild_id: int,
        user_id: int,
        channel_id: int,
        definition: ToyokoSearchDefinition,
        interval_min_seconds: int,
        interval_max_seconds: int,
        now: float,
    ) -> tuple[ToyokoRule, bool]:
        query = definition.query
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO toyoko_watch_rules ("
            "guild_id,user_id,channel_id,search_url,destination,scope_type,scope_id,"
            "check_in,check_out,people,rooms,smoking,interval_min_seconds,"
            "interval_max_seconds,hotels_json,next_check_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                guild_id,
                user_id,
                channel_id,
                query.search_url,
                definition.destination,
                query.scope_type,
                query.scope_id,
                query.check_in.isoformat(),
                query.check_out.isoformat(),
                query.people,
                query.rooms,
                query.smoking,
                interval_min_seconds,
                interval_max_seconds,
                self._hotels_json(definition.hotels),
                now,
            ),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT * FROM toyoko_watch_rules "
            "WHERE guild_id=? AND user_id=? AND search_url=?",
            (guild_id, user_id, query.search_url),
        ).fetchone()
        if row is None:
            raise RuntimeError("Unable to read saved Toyoko rule")
        return self._row_to_rule(row), cursor.rowcount > 0

    def count_user_rules(self, guild_id: int, user_id: int) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM toyoko_watch_rules WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
        return int(row[0])

    def user_rules(self, guild_id: int, user_id: int) -> tuple[ToyokoRule, ...]:
        rows = self.connection.execute(
            "SELECT * FROM toyoko_watch_rules WHERE guild_id=? AND user_id=? "
            "ORDER BY check_in, id",
            (guild_id, user_id),
        ).fetchall()
        return tuple(self._row_to_rule(row) for row in rows)

    def due_rules(self, now: float) -> tuple[ToyokoRule, ...]:
        rows = self.connection.execute(
            "SELECT * FROM toyoko_watch_rules "
            "WHERE enabled=1 AND next_check_at<=? ORDER BY next_check_at,id",
            (now,),
        ).fetchall()
        return tuple(self._row_to_rule(row) for row in rows)

    def get_rule(self, rule_id: int) -> ToyokoRule | None:
        row = self.connection.execute(
            "SELECT * FROM toyoko_watch_rules WHERE id=?", (rule_id,)
        ).fetchone()
        return self._row_to_rule(row) if row is not None else None

    def set_enabled(self, rule_id: int, user_id: int, enabled: bool) -> bool:
        cursor = self.connection.execute(
            "UPDATE toyoko_watch_rules SET enabled=?, next_check_at=? "
            "WHERE id=? AND user_id=?",
            (int(enabled), datetime.now(timezone.utc).timestamp(), rule_id, user_id),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def delete_rule(self, rule_id: int, user_id: int) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM toyoko_watch_rules WHERE id=? AND user_id=?",
            (rule_id, user_id),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def schedule_now(self, rule_id: int, user_id: int, now: float) -> bool:
        cursor = self.connection.execute(
            "UPDATE toyoko_watch_rules SET enabled=1,next_check_at=? "
            "WHERE id=? AND user_id=?",
            (now, rule_id, user_id),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def record_success(
        self,
        rule: ToyokoRule,
        result: ToyokoAvailabilityResult,
        *,
        now: float,
    ) -> None:
        self.connection.execute(
            "UPDATE toyoko_watch_rules SET last_status=?,last_result_hash=?,"
            "last_available_json=?,last_checked_at=?,consecutive_errors=0,"
            "next_check_at=? WHERE id=?",
            (
                result.status,
                result.result_hash,
                json.dumps(result.snapshot, separators=(",", ":")),
                datetime.now(timezone.utc).isoformat(),
                now
                + next_interval(
                    rule.interval_min_seconds, rule.interval_max_seconds
                ),
                rule.rule_id,
            ),
        )
        self.connection.commit()

    def record_failure(
        self,
        rule: ToyokoRule,
        *,
        now: float,
        retry_after: int | None = None,
    ) -> None:
        failures = rule.consecutive_errors + 1
        delay = max(retry_after or 0, error_backoff_seconds(failures))
        self.connection.execute(
            "UPDATE toyoko_watch_rules SET consecutive_errors=?,last_checked_at=?,"
            "next_check_at=? WHERE id=?",
            (
                failures,
                datetime.now(timezone.utc).isoformat(),
                now + delay,
                rule.rule_id,
            ),
        )
        self.connection.commit()

    def disable_expired(self, today: date) -> int:
        cursor = self.connection.execute(
            "UPDATE toyoko_watch_rules SET enabled=0 "
            "WHERE enabled=1 AND check_in<=?",
            (today.isoformat(),),
        )
        self.connection.commit()
        return cursor.rowcount

    def get_setting(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM toyoko_settings WHERE key=?", (key,)
        ).fetchone()
        return str(row[0]) if row is not None else None

    def set_setting(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO toyoko_settings(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.connection.commit()

    def delete_setting(self, key: str) -> None:
        self.connection.execute("DELETE FROM toyoko_settings WHERE key=?", (key,))
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
