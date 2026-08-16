"""
Один ответ на одно сообщение.

У Гослинга два независимых входа в группу — свой телеграм-хендлер
(HUMAN_REPLY_CHANCE) и HTTP /task от Филли — и до 16.08 они не знали друг о
друге. В логе 08:41:57 «С добрым утром головы картонные!» пришло обоими путями
и получило ДВА ответа: 08:42:01 короткий и 08:42:06 монолог на пол-экрана.
У каждого пути был свой порог «отвечать ли» и ни у одного — потолок «не ответил
ли я уже».

Запуск:
    cd gosling-bot && python3 -m unittest discover -s tests -v
"""

import asyncio
import os
import sys
import unittest

os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("YOUR_TELEGRAM_ID", "391077101")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:1/0")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402


def run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


class FakeRedis:
    """Только SET NX EX — больше замку ничего не нужно."""

    def __init__(self, broken=False):
        self.keys, self.broken = {}, broken

    async def set(self, key, value, nx=False, ex=None):
        if self.broken:
            raise RuntimeError("redis лёг")
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    async def pipeline(self, *a, **k):        # log_event ходит сюда
        raise RuntimeError("не нужен в этом тесте")


class _Patched:
    """Подменяет redis_client на время прогона."""

    def __init__(self, redis):
        self.redis = redis

    def __enter__(self):
        self._orig = bot.redis_client
        bot.redis_client = self.redis
        return self.redis

    def __exit__(self, *a):
        bot.redis_client = self._orig
        return False


class TestAnswerKey(unittest.TestCase):
    """Ключ по тексту: у HTTP-пути message_id нет вовсе."""

    def test_same_text_same_key(self):
        self.assertEqual(bot._answer_key("С добрым утром"),
                         bot._answer_key("С добрым утром"))

    def test_whitespace_and_case_do_not_matter(self):
        # Телеграм-путь видит оригинал, Филли пересобирает — расхождение в
        # пробелах не должно расщеплять замок надвое.
        self.assertEqual(bot._answer_key("С добрым  утром\nголовы"),
                         bot._answer_key("с добрым утром головы"))

    def test_different_texts_differ(self):
        self.assertNotEqual(bot._answer_key("привет"), bot._answer_key("пока"))

    def test_key_is_namespaced_to_this_bot(self):
        self.assertTrue(bot._answer_key("x").startswith("office:answered:гослинг:"))


class TestClaimAnswer(unittest.TestCase):
    def test_first_claim_wins_second_is_refused(self):
        with _Patched(FakeRedis()):
            self.assertTrue(run(bot.claim_answer("С добрым утром головы картонные!")))
            self.assertFalse(run(bot.claim_answer("С добрым утром головы картонные!")))

    def test_different_messages_do_not_block_each_other(self):
        with _Patched(FakeRedis()):
            self.assertTrue(run(bot.claim_answer("первое")))
            self.assertTrue(run(bot.claim_answer("второе")))

    def test_no_redis_is_fail_open(self):
        # Лучше два ответа, чем ни одного: болталка не тот путь, ради которого
        # стоит молчать из-за Redis.
        with _Patched(None):
            self.assertTrue(run(bot.claim_answer("текст")))
            self.assertTrue(run(bot.claim_answer("текст")))

    def test_broken_redis_is_fail_open(self):
        with _Patched(FakeRedis(broken=True)):
            self.assertTrue(run(bot.claim_answer("текст")))

    def test_empty_text_is_not_locked(self):
        with _Patched(FakeRedis()):
            self.assertTrue(run(bot.claim_answer("")))


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class TestHandleTaskDedup(unittest.IsolatedAsyncioTestCase):
    """HTTP-путь молчит, если это же сообщение уже забрал телеграм-путь."""

    async def asyncSetUp(self):
        self.generated = []
        self.sent = []
        self._orig = (bot.generate_response, bot.send_to_group,
                      bot.log_event, bot.redis_client)

        async def fake_generate(message, user_id, **kw):
            self.generated.append(message)
            return "ответ Гослинга"

        async def fake_send(text):
            self.sent.append(text)

        async def fake_log_event(*a, **k):
            pass

        bot.generate_response = fake_generate
        bot.send_to_group = fake_send
        bot.log_event = fake_log_event
        bot.redis_client = FakeRedis()

    async def asyncTearDown(self):
        (bot.generate_response, bot.send_to_group,
         bot.log_event, bot.redis_client) = self._orig

    async def test_first_http_call_answers(self):
        r = await bot.handle_task(_FakeRequest({"message": "Чё за идеи?"}))
        self.assertEqual(self.generated, ["Чё за идеи?"])
        self.assertEqual(self.sent, ["ответ Гослинга"])
        self.assertEqual(r.status, 200)

    async def test_second_call_on_the_same_text_is_silent(self):
        await bot.claim_answer("С добрым утром головы картонные!")   # телеграм-путь успел
        await bot.handle_task(_FakeRequest(
            {"message": "С добрым утром головы картонные!"}))
        self.assertEqual(self.generated, [], "сгенерировал второй ответ")
        self.assertEqual(self.sent, [], "запостил второй ответ в группу")

    async def test_duplicate_still_answers_200_to_filly(self):
        # Филли доставил — это не его ошибка. 500 увёл бы разбор не туда.
        await bot.claim_answer("дубль")
        r = await bot.handle_task(_FakeRequest({"message": "дубль"}))
        self.assertEqual(r.status, 200)

    async def test_banter_ping_is_never_deduped(self):
        # Реплика болталки — отдельное высказывание, а не второй заход на то же
        # сообщение. Замок бы глушил её ровно там, где две реплики и задуманы.
        await bot.claim_answer("[Болталка] что там")
        await bot.handle_task(_FakeRequest(
            {"message": "[Болталка] что там", "source": "BANTER", "depth": 1}))
        self.assertEqual(self.generated, ["[Болталка] что там"])
        self.assertEqual(self.sent, ["ответ Гослинга"])


if __name__ == "__main__":
    unittest.main()
