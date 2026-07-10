import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app import Config, HinataWatcher, image_batches, member_mention_ids
from blog_watcher import (
    BlogPost,
    BlogImageDownloader,
    StateStore,
    post_directory,
    safe_path_component,
    parse_hinata_blog,
    parse_blog_index,
    parse_sakura_blog,
)


HINATA_BLOG_HTML = """
<html><head><title>公式ブログ</title></head><body>
<div class="l-maincontents--blog"><div class="p-blog-article">
  <div class="c-blog-article__title">測試標題</div>
  <div class="c-blog-article__name"><a>測試作者</a></div>
  <time>2026.7.10 12:00</time>
  <div class="c-blog-article__text">
    <img src="/images/a.jpg"><img src="https://cdn.example/b.jpg">
    <img src="/images/a.jpg">
  </div>
</div></div>
<img src="/navigation-should-not-be-included.jpg">
</body></html>
"""

HINATA_INDEX_HTML = """
<html><body>
  <a href="/s/official/diary/detail/70153?ima=0000">newest</a>
  <a href="/s/official/diary/detail/70150?ima=0000">second</a>
  <a href="/s/official/diary/detail/70153?duplicate=1">duplicate</a>
  <a href="/s/official/news/detail/O123">not a blog</a>
  <a href="https://www.hinatazaka46.com/s/official/diary/detail/70143">third</a>
</body></html>
"""

SAKURA_BLOG_HTML = """
<html><head><title>公式ブログ</title></head><body>
<article class="post">
  <div class="title-wrap"><h1 class="title">櫻の標題</h1></div>
  <div class="box-article">
    <img src="/files/14/diary/s46/blog/a.jpg">
    <img src="https://cdn.example/sakura-b.jpg">
  </div>
  <div class="blog-foot"><p class="name">的野 美青</p>
    <p class="date">2026/07/07 20:39</p></div>
</article>
</body></html>
"""


class ParserTests(unittest.TestCase):
    def test_extracts_unique_homepage_ids_in_display_order(self):
        self.assertEqual(
            parse_blog_index(HINATA_INDEX_HTML), (70153, 70150, 70143)
        )

    def test_empty_homepage_has_no_ids(self):
        self.assertEqual(parse_blog_index(""), ())

    def test_extracts_sakura_blog_metadata_and_images(self):
        post = parse_sakura_blog(70124, SAKURA_BLOG_HTML)
        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post.title, "櫻の標題")
        self.assertEqual(post.author, "的野 美青")
        self.assertEqual(post.published_at, "2026/07/07 20:39")
        self.assertEqual(
            post.image_urls,
            (
                "https://sakurazaka46.com/files/14/diary/s46/blog/a.jpg",
                "https://cdn.example/sakura-b.jpg",
            ),
        )

    def test_sakura_empty_title_uses_untitled(self):
        post = parse_sakura_blog(70124, SAKURA_BLOG_HTML.replace("櫻の標題", ""))
        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post.title, "無題")

    def test_extracts_only_unique_article_images(self):
        post = parse_hinata_blog(70143, HINATA_BLOG_HTML)
        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post.title, "測試標題")
        self.assertEqual(post.author, "測試作者")
        self.assertEqual(
            post.image_urls,
            (
                "https://www.hinatazaka46.com/images/a.jpg",
                "https://cdn.example/b.jpg",
            ),
        )

    def test_rejects_empty_or_non_blog_pages(self):
        self.assertIsNone(parse_hinata_blog(1, ""))
        self.assertIsNone(parse_hinata_blog(1, "<html><body>news</body></html>"))
        self.assertIsNone(
            parse_hinata_blog(1, "<html><body>ブログ only</body></html>")
        )


class LocalPathTests(unittest.TestCase):
    def test_folder_contains_member_date_title_and_id(self):
        post = BlogPost(
            post_id=70143,
            url="https://example.test/70143",
            title='標題：夏天/測試?*',
            author="高井 俐香",
            published_at="2026.7.9 14:37",
            image_urls=(),
        )
        path = post_directory(Path("images"), post)
        self.assertEqual(path.parent.name, "高井 俐香")
        self.assertEqual(path.name, "2026-07-09_標題_夏天_測試___70143")

    def test_windows_reserved_name_is_made_safe(self):
        self.assertEqual(safe_path_component("CON", "fallback"), "_CON")

    def test_download_filename_keeps_image_extension(self):
        self.assertEqual(
            BlogImageDownloader._filename(2, "https://cdn.example/photo.JPG?size=large"),
            "02_photo.jpg",
        )

    def test_discord_images_are_batched_at_ten_embeds(self):
        urls = [f"https://cdn.test/{index}.jpg" for index in range(23)]
        batches = image_batches(urls)
        self.assertEqual([len(batch) for batch in batches], [10, 10, 3])


class ConfigTests(unittest.TestCase):
    @staticmethod
    def _minimum_env() -> dict[str, str]:
        return {
            "DISCORD_BOT_TOKEN": "test-token",
            "HINATA_DISCORD_CHANNEL_ID": "123456789012345678",
            "SAKURA_DISCORD_CHANNEL_ID": "123456789012345679",
        }

    def test_local_image_storage_defaults_to_enabled(self):
        with patch.dict("os.environ", self._minimum_env(), clear=True):
            self.assertTrue(Config.from_env().save_images_locally)

    def test_local_image_storage_can_be_disabled(self):
        environment = self._minimum_env() | {"SAVE_IMAGES_LOCALLY": "false"}
        with patch.dict("os.environ", environment, clear=True):
            self.assertFalse(Config.from_env().save_images_locally)

    def test_local_image_storage_rejects_invalid_value(self):
        environment = self._minimum_env() | {"SAVE_IMAGES_LOCALLY": "sometimes"}
        with patch.dict("os.environ", environment, clear=True):
            with self.assertRaisesRegex(ValueError, "SAVE_IMAGES_LOCALLY"):
                Config.from_env()

    def test_disabled_storage_does_not_create_image_downloader(self):
        environment = self._minimum_env() | {"SAVE_IMAGES_LOCALLY": "false"}
        with patch.dict("os.environ", environment, clear=True):
            config = Config.from_env()
        watcher = SimpleNamespace(
            http_session=object(),
            config=config,
            is_closed=lambda: True,
        )
        with patch("app.BlogImageDownloader") as downloader:
            asyncio.run(
                HinataWatcher.watch_forever(watcher, config.sources[0], object())
            )
        downloader.assert_not_called()

    def test_each_group_has_independent_member_mentions(self):
        environment = self._minimum_env() | {
            "HINATA_MEMBER_MENTIONS": (
                '{"高井 俐香":["111111111111111111","222222222222222222"]}'
            ),
            "SAKURA_MEMBER_MENTIONS": '{"的野 美青":"333333333333333333"}',
        }
        with patch.dict("os.environ", environment, clear=True):
            config = Config.from_env()

        self.assertEqual(
            member_mention_ids(config.sources[0], "高井　俐香"),
            (111111111111111111, 222222222222222222),
        )
        self.assertEqual(
            member_mention_ids(config.sources[1], "的野 美青"),
            (333333333333333333,),
        )
        self.assertEqual(member_mention_ids(config.sources[0], "的野 美青"), ())

    def test_member_mentions_reject_invalid_discord_id(self):
        environment = self._minimum_env() | {
            "HINATA_MEMBER_MENTIONS": '{"高井 俐香":["not-an-id"]}'
        }
        with patch.dict("os.environ", environment, clear=True):
            with self.assertRaisesRegex(ValueError, "HINATA_MEMBER_MENTIONS"):
                Config.from_env()


class StateTests(unittest.TestCase):
    def test_discord_skip_requires_header_and_every_image(self):
        post = BlogPost(
            post_id=200,
            url="https://example.test/200",
            title="title",
            author="member",
            published_at="2026.7.10 12:00",
            image_urls=("https://cdn.test/a.jpg", "https://cdn.test/b.jpg"),
        )
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.db")
            watcher = object.__new__(HinataWatcher)
            self.assertFalse(watcher.already_sent(state, post))
            state.mark_post_announced(200)
            state.mark_image_sent(200, post.image_urls[0])
            self.assertFalse(watcher.already_sent(state, post))
            state.mark_image_sent(200, post.image_urls[1])
            self.assertTrue(watcher.already_sent(state, post))
            state.close()

    def test_completed_post_and_index_mode_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            state = StateStore(path)
            self.assertFalse(state.index_initialized)
            self.assertFalse(state.post_completed(123))
            state.mark_index_initialized()
            state.mark_post_completed(123)
            state.close()

            reopened = StateStore(path)
            self.assertTrue(reopened.index_initialized)
            self.assertTrue(reopened.post_completed(123))
            reopened.close()

    def test_reactivates_only_baseline_posts_never_announced(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.db")
            state.mark_post_completed(100)
            state.mark_post_completed(101)
            state.mark_post_announced(101)
            self.assertEqual(state.enable_previously_ignored_posts(), 1)
            self.assertFalse(state.post_completed(100))
            self.assertTrue(state.post_completed(101))
            self.assertEqual(state.enable_previously_ignored_posts(), 0)
            state.close()


class MentionNotificationTests(unittest.TestCase):
    def test_only_first_discord_batch_mentions_configured_users(self):
        environment = ConfigTests._minimum_env() | {
            "HINATA_MEMBER_MENTIONS": '{"高井 俐香":["111111111111111111"]}'
        }
        with patch.dict("os.environ", environment, clear=True):
            source = Config.from_env().sources[0]

        post = BlogPost(
            post_id=70143,
            url="https://example.test/70143",
            title="title",
            author="高井 俐香",
            published_at="2026.7.10 12:00",
            image_urls=tuple(
                f"https://cdn.test/{index}.jpg" for index in range(11)
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.db")
            watcher = object.__new__(HinataWatcher)
            watcher.log = Mock()
            channel = SimpleNamespace(send=AsyncMock())
            watcher.get_target_channel = AsyncMock(return_value=channel)
            watcher.reconcile_discord_history = AsyncMock()

            with patch("app.asyncio.sleep", new=AsyncMock()):
                asyncio.run(watcher.announce(source, state, post))

            self.assertEqual(channel.send.await_count, 2)
            first_call, second_call = channel.send.await_args_list
            self.assertTrue(
                first_call.kwargs["content"].startswith("<@111111111111111111>\n")
            )
            self.assertEqual(
                first_call.kwargs["allowed_mentions"].to_dict()["users"],
                [111111111111111111],
            )
            self.assertIsNone(second_call.kwargs["content"])
            self.assertNotIn(
                "users", second_call.kwargs["allowed_mentions"].to_dict()
            )
            state.close()


if __name__ == "__main__":
    unittest.main()
