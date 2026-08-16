import asyncio
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from app import MultiBlogWatcher, ToyokoPanelView
from toyoko_watcher import (
    AVAILABLE,
    UNKNOWN,
    UNAVAILABLE,
    ToyokoAvailabilityResult,
    ToyokoClient,
    ToyokoHotelAvailability,
    ToyokoHotelTarget,
    ToyokoSearchDefinition,
    ToyokoStore,
    build_toyoko_search_url,
    error_backoff_seconds,
    parse_interval_minutes,
    parse_toyoko_price_response,
    parse_toyoko_search_page,
    parse_toyoko_search_url,
    should_notify_availability,
)


TODAY = date(2026, 8, 17)
SEARCH_URL = (
    "https://www.toyoko-inn.com/china/search/result/?"
    "prefecture=28&people=1&room=1&smoking=noSmoking&"
    "start=2026-11-25&end=2026-11-26"
)


def search_html() -> str:
    payload = {
        "props": {
            "pageProps": {
                "searchResponse": {
                    "area": None,
                    "prefecture": {"id": 28, "name": "兵庫縣"},
                    "spot": None,
                    "hotels": [
                        {"hotelCode": "00196", "name": "東橫INN 神戶湊川公園"},
                        {"hotelCode": "00304", "name": "東橫INN 神戶三宮站市役所前"},
                        {"hotelCode": "00196", "name": "duplicate"},
                    ],
                }
            }
        }
    }
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></body></html>"
    )


def price_payload(
    first_available: bool = True, second_available: bool = False
) -> list[dict]:
    return [
        {
            "result": {
                "data": {
                    "json": {
                        "prices": {
                            "00196": {
                                "lowestPrice": 7700 if first_available else 0,
                                "existEnoughVacantRooms": first_available,
                                "isUnderMaintenance": False,
                            },
                            "00304": {
                                "lowestPrice": 8900 if second_available else 0,
                                "existEnoughVacantRooms": second_available,
                                "isUnderMaintenance": False,
                            },
                        }
                    }
                }
            }
        }
    ]


class ToyokoURLTests(unittest.TestCase):
    def test_parses_and_canonicalizes_official_search_url(self):
        query = parse_toyoko_search_url(SEARCH_URL, today=TODAY)
        self.assertEqual(query.scope_type, "prefecture")
        self.assertEqual(query.scope_id, "28")
        self.assertEqual(query.nights, 1)
        self.assertEqual(query.people, 1)
        self.assertEqual(query.rooms, 1)
        self.assertEqual(query.smoking, "noSmoking")
        self.assertIn("prefecture=28", query.search_url)

    def test_builder_accepts_slash_dates(self):
        query = build_toyoko_search_url(
            scope_type="area",
            scope_id=523,
            check_in="2026/11/21",
            check_out="2026/11/23",
            people="1",
            rooms="1",
            smoking="noSmoking",
            today=TODAY,
        )
        self.assertEqual(query.scope_type, "area")
        self.assertEqual(query.nights, 2)
        self.assertIn("area=523", query.search_url)

    def test_rejects_foreign_host_or_non_result_page(self):
        with self.assertRaisesRegex(ValueError, "官方搜尋網址"):
            parse_toyoko_search_url(
                SEARCH_URL.replace("www.toyoko-inn.com", "example.com"),
                today=TODAY,
            )
        with self.assertRaisesRegex(ValueError, "搜尋結果網址"):
            parse_toyoko_search_url(
                "https://www.toyoko-inn.com/china/", today=TODAY
            )

    def test_rejects_multiple_scopes_and_invalid_dates(self):
        with self.assertRaisesRegex(ValueError, "一個飯店、地區或都道府縣"):
            parse_toyoko_search_url(
                SEARCH_URL + "&area=523", today=TODAY
            )
        with self.assertRaisesRegex(ValueError, "退房日期"):
            parse_toyoko_search_url(
                SEARCH_URL.replace("end=2026-11-26", "end=2026-11-24"),
                today=TODAY,
            )

    def test_rejects_room_count_above_people(self):
        with self.assertRaisesRegex(ValueError, "房間數不能大於"):
            parse_toyoko_search_url(
                SEARCH_URL.replace("room=1", "room=2"), today=TODAY
            )

    def test_interval_range_is_validated(self):
        self.assertEqual(
            parse_interval_minutes(
                "10-15",
                default_min_seconds=600,
                default_max_seconds=900,
            ),
            (600, 900),
        )
        self.assertEqual(
            parse_interval_minutes(
                "",
                default_min_seconds=900,
                default_max_seconds=1200,
            ),
            (900, 1200),
        )
        with self.assertRaisesRegex(ValueError, "不得低於"):
            parse_interval_minutes(
                "5-10",
                default_min_seconds=600,
                default_max_seconds=900,
            )


class ToyokoParserTests(unittest.TestCase):
    def setUp(self):
        self.query = parse_toyoko_search_url(SEARCH_URL, today=TODAY)
        self.hotels = (
            ToyokoHotelTarget("00196", "東橫INN 神戶湊川公園"),
            ToyokoHotelTarget("00304", "東橫INN 神戶三宮站市役所前"),
        )

    def test_search_page_extracts_destination_and_unique_hotels(self):
        definition = parse_toyoko_search_page(search_html(), self.query)
        self.assertEqual(definition.destination, "兵庫縣")
        self.assertEqual(definition.hotels, self.hotels)

    def test_prefecture_id_is_mapped_when_page_omits_its_name(self):
        html = search_html().replace(
            '"prefecture": {"id": 28, "name": "兵庫縣"}',
            '"prefecture": {"id": 28}',
        )
        definition = parse_toyoko_search_page(html, self.query)
        self.assertEqual(definition.destination, "兵庫縣")

    def test_search_page_without_next_data_is_unknown_not_no_room(self):
        with self.assertRaisesRegex(RuntimeError, "結構化資料"):
            parse_toyoko_search_page("<html></html>", self.query)

    def test_available_and_unavailable_prices(self):
        available = parse_toyoko_price_response(price_payload(), self.hotels)
        self.assertEqual(available.status, AVAILABLE)
        self.assertEqual(available.available_hotels[0].lowest_price, 7700)

        unavailable = parse_toyoko_price_response(
            price_payload(False, False), self.hotels
        )
        self.assertEqual(unavailable.status, UNAVAILABLE)
        self.assertEqual(unavailable.available_hotels, ())

    def test_missing_hotel_result_is_unknown(self):
        payload = price_payload(False, False)
        del payload[0]["result"]["data"]["json"]["prices"]["00304"]
        result = parse_toyoko_price_response(payload, self.hotels)
        self.assertEqual(result.status, UNKNOWN)

    def test_notification_only_for_new_availability_or_lower_price(self):
        result = parse_toyoko_price_response(price_payload(), self.hotels)
        self.assertTrue(should_notify_availability(None, {}, result))
        self.assertTrue(
            should_notify_availability(UNAVAILABLE, {}, result)
        )
        self.assertFalse(
            should_notify_availability(AVAILABLE, {"00196": 7700}, result)
        )
        self.assertTrue(
            should_notify_availability(AVAILABLE, {"00196": 8000}, result)
        )

    def test_client_chunks_more_than_ten_hotels(self):
        hotels = tuple(
            ToyokoHotelTarget(f"{index:05d}", f"Hotel {index}")
            for index in range(11)
        )

        def response_for(chunk):
            return json.dumps(
                [
                    {
                        "result": {
                            "data": {
                                "json": {
                                    "prices": {
                                        hotel.hotel_code: {
                                            "lowestPrice": 0,
                                            "existEnoughVacantRooms": False,
                                            "isUnderMaintenance": False,
                                        }
                                        for hotel in chunk
                                    }
                                }
                            }
                        }
                    }
                ]
            )

        client = object.__new__(ToyokoClient)
        client.request_gap_seconds = 3
        client._get_text = AsyncMock(
            side_effect=[response_for(hotels[:10]), response_for(hotels[10:])]
        )

        async def run():
            with patch("toyoko_watcher.asyncio.sleep", new=AsyncMock()) as sleep:
                result = await client.fetch_availability(self.query, hotels)
                self.assertEqual(result.status, UNAVAILABLE)
                self.assertEqual(len(result.hotels), 11)
                self.assertEqual(client._get_text.await_count, 2)
                sleep.assert_awaited_once_with(3)

        asyncio.run(run())


class ToyokoStoreTests(unittest.TestCase):
    def setUp(self):
        self.query = parse_toyoko_search_url(SEARCH_URL, today=TODAY)
        self.definition = ToyokoSearchDefinition(
            self.query,
            "兵庫縣",
            (
                ToyokoHotelTarget("00196", "東橫INN 神戶湊川公園"),
                ToyokoHotelTarget("00304", "東橫INN 神戶三宮站市役所前"),
            ),
        )

    def test_rule_crud_persists_and_duplicate_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "toyoko.db"
            store = ToyokoStore(path)
            first, created = store.add_rule(
                guild_id=1,
                user_id=2,
                channel_id=3,
                definition=self.definition,
                interval_min_seconds=600,
                interval_max_seconds=900,
                now=1000,
            )
            duplicate, duplicate_created = store.add_rule(
                guild_id=1,
                user_id=2,
                channel_id=3,
                definition=self.definition,
                interval_min_seconds=600,
                interval_max_seconds=900,
                now=1000,
            )
            self.assertTrue(created)
            self.assertFalse(duplicate_created)
            self.assertEqual(first.rule_id, duplicate.rule_id)
            self.assertEqual(store.count_user_rules(1, 2), 1)
            self.assertEqual(store.due_rules(1000)[0].destination, "兵庫縣")
            store.close()

            reopened = ToyokoStore(path)
            self.assertEqual(len(reopened.user_rules(1, 2)), 1)
            reopened.close()

    def test_success_dedup_backoff_pause_and_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ToyokoStore(Path(directory) / "toyoko.db")
            rule, _ = store.add_rule(
                guild_id=1,
                user_id=2,
                channel_id=3,
                definition=self.definition,
                interval_min_seconds=600,
                interval_max_seconds=600,
                now=1000,
            )
            result = ToyokoAvailabilityResult(
                AVAILABLE,
                (
                    ToyokoHotelAvailability(
                        "00196", "東橫INN 神戶湊川公園", True, 7700
                    ),
                ),
            )
            store.record_success(rule, result, now=1000)
            saved = store.get_rule(rule.rule_id)
            assert saved is not None
            self.assertEqual(saved.last_status, AVAILABLE)
            self.assertEqual(saved.last_available, {"00196": 7700})
            self.assertEqual(saved.next_check_at, 1600)

            store.record_failure(saved, now=2000)
            failed = store.get_rule(rule.rule_id)
            assert failed is not None
            self.assertEqual(failed.consecutive_errors, 1)
            self.assertEqual(failed.next_check_at, 3800)
            self.assertTrue(store.set_enabled(rule.rule_id, 2, False))
            self.assertFalse(store.get_rule(rule.rule_id).enabled)
            self.assertTrue(store.delete_rule(rule.rule_id, 2))
            self.assertEqual(store.user_rules(1, 2), ())
            store.close()

    def test_backoff_caps_at_six_hours(self):
        self.assertEqual(error_backoff_seconds(1), 1800)
        self.assertEqual(error_backoff_seconds(2), 3600)
        self.assertEqual(error_backoff_seconds(20), 21600)


class ToyokoDiscordTests(unittest.TestCase):
    def _rule_and_result(self):
        query = parse_toyoko_search_url(SEARCH_URL, today=TODAY)
        with tempfile.TemporaryDirectory() as directory:
            store = ToyokoStore(Path(directory) / "toyoko.db")
            rule, _ = store.add_rule(
                guild_id=1,
                user_id=2,
                channel_id=3,
                definition=ToyokoSearchDefinition(
                    query,
                    "兵庫縣",
                    (ToyokoHotelTarget("00196", "東橫INN 神戶湊川公園"),),
                ),
                interval_min_seconds=600,
                interval_max_seconds=900,
                now=1000,
            )
            store.close()
        result = ToyokoAvailabilityResult(
            AVAILABLE,
            (
                ToyokoHotelAvailability(
                    "00196", "東橫INN 神戶湊川公園", True, 7700
                ),
            ),
        )
        return rule, result

    def test_public_panel_is_persistent(self):
        view = ToyokoPanelView(SimpleNamespace(log=Mock()))
        self.assertTrue(view.is_persistent())
        view.stop()

    def test_panel_is_created_once_and_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ToyokoStore(Path(directory) / "toyoko.db")
            message = SimpleNamespace(
                id=123,
                pinned=False,
                author=SimpleNamespace(id=99),
                edit=AsyncMock(),
            )
            channel = SimpleNamespace(
                send=AsyncMock(return_value=message),
                fetch_message=AsyncMock(return_value=message),
            )
            watcher = SimpleNamespace(
                config=SimpleNamespace(
                    enable_toyoko_watcher=True,
                    toyoko_channel_id=456,
                ),
                toyoko_panel_lock=asyncio.Lock(),
                toyoko_store=store,
                require_toyoko_store=lambda: store,
                get_channel=lambda _channel_id: channel,
                fetch_channel=AsyncMock(),
                user=SimpleNamespace(id=99),
                pin_toyoko_panel=AsyncMock(),
                log=Mock(),
                _toyoko_panel_setting_key=(
                    lambda: "toyoko_panel_message_id:456"
                ),
            )

            async def run():
                await MultiBlogWatcher.ensure_toyoko_panel(watcher)
                await MultiBlogWatcher.ensure_toyoko_panel(watcher)

            try:
                asyncio.run(run())
                channel.send.assert_awaited_once()
                channel.fetch_message.assert_awaited_once_with(123)
                message.edit.assert_awaited_once()
            finally:
                store.close()

    def test_dm_failure_falls_back_to_single_user_mention(self):
        rule, result = self._rule_and_result()
        response = Mock(status=403, reason="Forbidden")
        user = SimpleNamespace(
            send=AsyncMock(
                side_effect=discord.Forbidden(
                    response, {"message": "Cannot send messages", "code": 50007}
                )
            )
        )
        channel = SimpleNamespace(send=AsyncMock())
        watcher = SimpleNamespace(
            get_user=lambda _user_id: user,
            fetch_user=AsyncMock(),
            get_channel=lambda _channel_id: channel,
            fetch_channel=AsyncMock(),
            log=Mock(),
        )

        asyncio.run(
            MultiBlogWatcher.notify_toyoko_available(watcher, rule, result)
        )

        channel.send.assert_awaited_once()
        kwargs = channel.send.await_args.kwargs
        self.assertTrue(kwargs["content"].startswith("<@2>"))
        self.assertFalse(kwargs["allowed_mentions"].everyone)
        self.assertFalse(kwargs["allowed_mentions"].roles)

    def test_successful_dm_does_not_write_to_public_channel(self):
        rule, result = self._rule_and_result()
        user = SimpleNamespace(send=AsyncMock())
        channel = SimpleNamespace(send=AsyncMock())
        watcher = SimpleNamespace(
            get_user=lambda _user_id: user,
            fetch_user=AsyncMock(),
            get_channel=lambda _channel_id: channel,
            fetch_channel=AsyncMock(),
            log=Mock(),
        )

        asyncio.run(
            MultiBlogWatcher.notify_toyoko_available(watcher, rule, result)
        )

        user.send.assert_awaited_once()
        channel.send.assert_not_awaited()

    def test_scheduler_serializes_distinct_searches_with_global_gap(self):
        first_query = parse_toyoko_search_url(SEARCH_URL, today=TODAY)
        second_query = build_toyoko_search_url(
            scope_type="area",
            scope_id=523,
            check_in="2026-11-21",
            check_out="2026-11-23",
            people=1,
            rooms=1,
            smoking="noSmoking",
            today=TODAY,
        )
        result = ToyokoAvailabilityResult(
            UNAVAILABLE,
            (ToyokoHotelAvailability("00196", "Hotel", False, None),),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = ToyokoStore(Path(directory) / "toyoko.db")
            for user_id, query, destination in (
                (2, first_query, "兵庫縣"),
                (3, second_query, "神戶"),
            ):
                store.add_rule(
                    guild_id=1,
                    user_id=user_id,
                    channel_id=4,
                    definition=ToyokoSearchDefinition(
                        query,
                        destination,
                        (ToyokoHotelTarget("00196", "Hotel"),),
                    ),
                    interval_min_seconds=600,
                    interval_max_seconds=900,
                    now=1000,
                )
            client = SimpleNamespace(fetch_availability=AsyncMock(return_value=result))
            watcher = SimpleNamespace(
                require_toyoko_store=lambda: store,
                require_toyoko_client=lambda: client,
                is_closed=Mock(side_effect=[False, True]),
                config=SimpleNamespace(toyoko_rule_gap_seconds=30),
                notify_toyoko_available=AsyncMock(),
                log=Mock(),
            )

            async def run():
                with (
                    patch("app.japan_today", return_value=TODAY),
                    patch("app.asyncio.sleep", new=AsyncMock()) as sleep,
                ):
                    await MultiBlogWatcher.watch_toyoko_forever(watcher)
                    self.assertEqual(client.fetch_availability.await_count, 2)
                    self.assertEqual(
                        [call.args[0] for call in sleep.await_args_list], [30, 5]
                    )

            try:
                asyncio.run(run())
            finally:
                store.close()
