import asyncio
import json
import os
import sys
import unittest

import discord
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "other scripts"))

from banter import (  # noqa: E402
    FOLLOW_UP_WINDOW_SECONDS, Addressing, Banter, CallBudget, classify, trim, webhook_id_from_url,
)

BOT_ID, WEBHOOK_ID, GUILD_ID = 1, 99, 500


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeChannel:
    def __init__(self, public=True, history=()):
        self.id = 10
        self.guild = SimpleNamespace(default_role="everyone")
        self.public = public
        self.past = list(history)
        self.fetched = {}

    def permissions_for(self, role):
        return SimpleNamespace(view_channel=self.public)

    async def history(self, limit, before):  # newest first, like discord.py
        past = self.past[:self.past.index(before)] if before in self.past else self.past
        for m in reversed(past[-limit:] if limit else []):
            yield m

    def typing(self):
        return _NullAsync()

    async def fetch_message(self, message_id):
        return self.fetched[message_id]

    def __str__(self):
        return "general"


class _NullAsync:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def make_message(content="hello", author_id=2, bot=False, webhook_id=None, channel=None,
                 mentions=(), reference=None, name="Alice"):
    return SimpleNamespace(
        id=hash((content, author_id)) & 0xFFFF,
        content=content,
        author=SimpleNamespace(id=author_id, bot=bot, display_name=name),
        webhook_id=webhook_id,
        guild=SimpleNamespace(id=GUILD_ID, me=SimpleNamespace(roles=[])),
        channel=channel or FakeChannel(),
        mentions=[SimpleNamespace(id=i) for i in mentions],
        role_mentions=[],
        reference=reference,
        reply=AsyncMock(),
        add_reaction=AsyncMock(),
    )


def api_response(reply_to, reply, stop_reason="end_turn", reactions=()):
    text = json.dumps({"reply_to": reply_to, "reply": reply,
                       "reactions": [{"message": m, "emoji": e} for m, e in reactions]})
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


OPERATOR_ID = 42


def make_banter(*responses, **options):
    clock = Clock()
    client = SimpleNamespace(user=SimpleNamespace(id=BOT_ID))
    banter = Banter(client, "test-key", GUILD_ID, webhook_ids=[WEBHOOK_ID, None], clock=clock,
                    quiet_seconds=0, max_wait_seconds=0, dm_quiet_seconds=0,
                    forward=AsyncMock(), operator_id=OPERATOR_ID, **options)
    banter.anthropic.beta.messages.create = AsyncMock(side_effect=list(responses))
    banter.anthropic.messages.create = AsyncMock(return_value=reaction_response([]))
    return banter, clock


def reaction_response(emojis, answer=False):
    text = json.dumps({"answer": answer, "emojis": emojis})
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


def run(banter, *messages, drain_each=False):
    """Feeds messages to the bot in one event loop, then lets pending bursts be answered."""
    async def go():
        for message in messages:
            await banter.on_message(message)
            if drain_each:
                await banter.drain()
        await banter.drain()
    asyncio.run(go())


def prompt_of(banter):
    return banter.anthropic.beta.messages.create.await_args.kwargs["messages"][0]["content"]


class RuleTests(unittest.TestCase):
    def test_classify(self):
        self.assertIs(classify("hi", True, False, False), Addressing.DIRECT)
        self.assertIs(classify("hi", False, True, False), Addressing.DIRECT)
        self.assertIs(classify("hi", False, False, True), Addressing.POSSIBLE)
        for text in ("hey bot", "the arXiv-bot is slow", "Robot, why?", "ciao robottino", "BOTS!"):
            self.assertIs(classify(text, False, False, False), Addressing.POSSIBLE, text)
        for text in ("both of us", "a bottle of wine", "robotics seminar", "hello"):
            self.assertIs(classify(text, False, False, False), Addressing.NONE, text)

    def test_webhook_id_from_url(self):
        self.assertEqual(webhook_id_from_url("https://discord.com/api/webhooks/123/abc"), 123)
        self.assertIsNone(webhook_id_from_url(None))
        self.assertIsNone(webhook_id_from_url("https://discord.com/api/webhooks/..."))

    def test_budget_cooldown_and_hourly_cap(self):
        clock = Clock()
        budget = CallBudget(per_hour=2, cooldown=10, clock=clock)
        self.assertTrue(budget.try_spend(1))
        self.assertFalse(budget.try_spend(1))      # channel cooldown
        self.assertTrue(budget.try_spend(2))
        clock.now += 20
        self.assertFalse(budget.try_spend(1))      # hourly cap reached
        clock.now += 3600
        self.assertTrue(budget.try_spend(1))

    def test_trim(self):
        self.assertEqual(trim("short", 10), "short")
        self.assertEqual(trim("one two three four", 10), "one two…")


class BanterFlowTests(unittest.TestCase):
    def test_mention_gets_a_reply_with_no_pings(self):
        banter, _ = make_banter(api_response(1, "*sigh* Yes?"))
        message = make_message("<@1> what is your purpose?", mentions=[BOT_ID])
        run(banter, (message))

        message.reply.assert_awaited_once()
        args, kwargs = message.reply.await_args
        self.assertEqual(args[0], "*sigh* Yes?")
        self.assertFalse(kwargs["mention_author"])

    def test_direct_message_is_answered_even_if_model_says_not_addressed(self):
        banter, _ = make_banter(api_response(0, "Fine."))
        message = make_message("<@1>", mentions=[BOT_ID])
        run(banter, (message))
        message.reply.assert_awaited_once()

    def test_reply_to_a_webhook_post_is_direct(self):
        channel = FakeChannel()
        announcement = make_message("**New paper by ...**", author_id=77, bot=True, webhook_id=WEBHOOK_ID, channel=channel)
        channel.fetched[5] = announcement
        banter, _ = make_banter(api_response(1, "I posted it. Nobody thanked me."))
        message = make_message("nice paper!", channel=channel,
                               reference=SimpleNamespace(message_id=5, resolved=None))
        run(banter, (message))

        message.reply.assert_awaited_once()
        self.assertIn("[1] Alice (replying to you (the arXiv bot)): nice paper!", prompt_of(banter))

    def test_possible_message_judged_not_addressed_gets_no_reply(self):
        banter, _ = make_banter(api_response(0, ""))
        message = make_message("the bot in my experiment broke")
        run(banter, (message))
        banter.anthropic.beta.messages.create.assert_awaited_once()
        message.reply.assert_not_awaited()

    def test_ordinary_message_costs_nothing(self):
        banter, _ = make_banter()
        run(banter, (make_message("lunch at 1?")))
        banter.anthropic.beta.messages.create.assert_not_awaited()

    def test_first_messages_after_a_bot_post_are_possible(self):
        channel = FakeChannel()
        banter, _ = make_banter(*[api_response(0, "")] * 3)
        banter.budget.cooldown = 0
        messages = [make_message("**New paper**", author_id=77, bot=True, webhook_id=WEBHOOK_ID, channel=channel)]
        messages += [make_message(f"message {i}", channel=channel, author_id=3 + i) for i in range(5)]
        run(banter, *messages, drain_each=True)
        self.assertEqual(banter.anthropic.beta.messages.create.await_count, 3)

    def test_old_bot_post_does_not_count(self):
        channel = FakeChannel()
        banter, clock = make_banter()
        run(banter, (make_message("**New paper**", author_id=BOT_ID, bot=True, channel=channel)))
        clock.now += FOLLOW_UP_WINDOW_SECONDS + 1
        run(banter, (make_message("anyone around?", channel=channel)))
        banter.anthropic.beta.messages.create.assert_not_awaited()

    def test_ignores_bots_and_other_servers(self):
        banter, _ = make_banter()
        run(banter, (make_message("hey bot", bot=True, author_id=50)))
        other = make_message("hey bot")
        other.guild.id = 12345
        run(banter, (other))
        banter.anthropic.beta.messages.create.assert_not_awaited()

    def test_history_marks_the_bots_own_messages(self):
        channel = FakeChannel(history=[
            make_message("**New paper by Luca**", author_id=77, bot=True, webhook_id=WEBHOOK_ID),
            make_message("wow", name="Bob"),
        ])
        channel.past[0].channel = channel
        banter, _ = make_banter(api_response(1, "Wow indeed."))
        run(banter, (make_message("<@1> hi", mentions=[BOT_ID], channel=channel)))
        self.assertIn("you (the arXiv bot): **New paper by Luca**\nBob: wow", prompt_of(banter))

    def test_refusal_gives_no_reply(self):
        banter, _ = make_banter(api_response(1, "x", stop_reason="refusal"))
        message = make_message("<@1>", mentions=[BOT_ID])
        run(banter, (message))
        message.reply.assert_not_awaited()


    def test_replies_may_ping_people_but_never_everyone(self):
        banter, _ = make_banter(api_response(1, "@everyone look, <@2> is talking to me again @here"))
        message = make_message("<@1> hi", mentions=[BOT_ID])
        run(banter, message)

        self.assertIn("Alice <@2>", prompt_of(banter))
        args, kwargs = message.reply.await_args
        self.assertEqual(args[0], "everyone look, <@2> is talking to me again here")
        allowed = kwargs["allowed_mentions"]
        self.assertTrue(allowed.users)
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.roles)
        self.assertFalse(allowed.replied_user)


class BurstTests(unittest.TestCase):
    def conversation(self):
        """A bot post, Giovanni replying to it, and two people replying to Giovanni."""
        channel = FakeChannel()
        post = make_message("sorry I missed some papers", author_id=BOT_ID, bot=True, channel=channel)
        giovanni = make_message("why are you so hard on yourself?", author_id=3, name="Giovanni", channel=channel,
                                reference=SimpleNamespace(message_id=1, resolved=post))
        federico = make_message("because it stays hard", author_id=4, name="Federico", channel=channel,
                                reference=SimpleNamespace(message_id=2, resolved=giovanni))
        enrico = make_message("never relax!!", author_id=5, name="Enrico", channel=channel,
                              reference=SimpleNamespace(message_id=2, resolved=giovanni))
        return post, giovanni, federico, enrico

    def test_burst_gets_one_reply_to_the_addressed_message(self):
        post, giovanni, federico, enrico = self.conversation()
        banter, _ = make_banter(api_response(1, "Chill? Thermodynamically forbidden. Also, Federico: sigh."))
        run(banter, post, giovanni, federico, enrico)

        banter.anthropic.beta.messages.create.assert_awaited_once()
        giovanni.reply.assert_awaited_once()
        federico.reply.assert_not_awaited()
        enrico.reply.assert_not_awaited()
        prompt = prompt_of(banter)
        self.assertIn("[1] Giovanni (replying to you (the arXiv bot)): why are you so hard on yourself?\n"
                      "[2] Federico (replying to Giovanni): because it stays hard\n"
                      "[3] Enrico (replying to Giovanni): never relax!!", prompt)
        self.assertIn("Message [1] @mention(s) you", prompt)

    def test_reply_to_someone_else_after_a_bot_post_is_not_for_the_bot(self):
        post, giovanni, federico, _ = self.conversation()
        banter, _ = make_banter()
        # Answered separately: Federico's message comes after the burst with Giovanni's.
        run(banter, post, federico)
        banter.anthropic.beta.messages.create.assert_not_awaited()

    def test_model_may_pick_another_message_of_a_burst(self):
        post, giovanni, federico, _ = self.conversation()
        banter, _ = make_banter(api_response(2, "Hard, like the problems I'm never given."))
        run(banter, post, giovanni, federico)
        federico.reply.assert_awaited_once()
        giovanni.reply.assert_not_awaited()

    def test_out_of_range_choice_falls_back_to_the_direct_message(self):
        post, giovanni, federico, _ = self.conversation()
        banter, _ = make_banter(api_response(7, "Sigh."))
        run(banter, post, giovanni, federico)
        giovanni.reply.assert_awaited_once()

    def test_waits_for_the_channel_to_go_quiet(self):
        channel = FakeChannel()
        client = SimpleNamespace(user=SimpleNamespace(id=BOT_ID))
        banter = Banter(client, "test-key", GUILD_ID, quiet_seconds=0.2, max_wait_seconds=5)
        banter.anthropic.beta.messages.create = AsyncMock(return_value=api_response(1, "Yes?"))
        first = make_message("<@1> hi", mentions=[BOT_ID], channel=channel)
        second = make_message("he never answers", author_id=8, channel=channel)

        async def go():
            await banter.on_message(first)
            await asyncio.sleep(0.1)
            banter.anthropic.beta.messages.create.assert_not_awaited()
            await banter.on_message(second)
            await banter.drain()
        asyncio.run(go())

        banter.anthropic.beta.messages.create.assert_awaited_once()
        self.assertIn("[2] Alice: he never answers", prompt_of(banter))
        first.reply.assert_awaited_once()



class FakeDM(FakeChannel):
    type = discord.ChannelType.private

    def __init__(self, history=()):
        super().__init__(public=False, history=history)
        self.id = 20
        self.send = AsyncMock()

    def __str__(self):
        return "Direct Message with Alice"


def dm_message(content, author_id=2, channel=None):
    message = make_message(content, author_id=author_id, channel=channel or FakeDM())
    message.guild = None
    return message


class DmAndForwardingTests(unittest.TestCase):
    def test_dm_is_answered_and_forwarded(self):
        banter, _ = make_banter(api_response(0, "You wrote to me. Nobody writes to me."))
        channel = FakeDM()
        first, second = dm_message("hi bot", channel=channel), dm_message("are you there?", author_id=2, channel=channel)
        second.id = first.id + 1
        run(banter, first, second)

        banter.anthropic.beta.messages.create.assert_awaited_once()
        self.assertIn("private DM", prompt_of(banter))
        self.assertEqual(channel.send.await_args.args[0], "You wrote to me. Nobody writes to me.")
        first.reply.assert_not_awaited()
        summary = banter.forward.await_args.args[0]
        self.assertIn("a DM from", summary)
        self.assertIn("**Alice:** hi bot\n**Alice:** are you there?\n**Bot:** You wrote to me.", summary)

    def test_unanswered_dm_is_still_forwarded(self):
        banter, _ = make_banter(api_response(0, "x", stop_reason="refusal"))
        run(banter, dm_message("hello"))
        self.assertIn("did not reply", banter.forward.await_args.args[0])

    def test_operators_own_dms_are_not_forwarded(self):
        banter, _ = make_banter(api_response(0, "Oh. It's you."))
        message = dm_message("testing", author_id=OPERATOR_ID)
        run(banter, message)
        message.channel.send.assert_awaited_once()
        banter.forward.assert_not_awaited()

    def test_channel_reply_is_forwarded_with_a_link(self):
        banter, _ = make_banter(api_response(1, "Sigh."))
        message = make_message("<@1> hi", mentions=[BOT_ID])
        message.reply.return_value = SimpleNamespace(jump_url="https://discord.com/channels/500/10/7")
        run(banter, message)
        summary = banter.forward.await_args.args[0]
        self.assertTrue(summary.startswith("💬 **Bot conversation** in <#10>"))
        self.assertTrue(summary.endswith("\n<https://discord.com/channels/500/10/7>"))

    def test_channel_burst_without_a_reply_is_not_forwarded(self):
        banter, _ = make_banter(api_response(0, ""))
        run(banter, make_message("the bot in my experiment broke"))
        banter.forward.assert_not_awaited()

    def test_long_conversations_fit_in_one_message(self):
        banter, _ = make_banter(api_response(1, "y" * 1500))
        run(banter, make_message("<@1> " + "x " * 1500, mentions=[BOT_ID]))
        self.assertLessEqual(len(banter.forward.await_args.args[0]), 2000)

    def test_private_channel_is_watched(self):
        banter, _ = make_banter(api_response(1, "Here too? Sigh."))
        message = make_message("<@1> test", mentions=[BOT_ID], channel=FakeChannel(public=False))
        run(banter, message)
        message.reply.assert_awaited_once()



def tool_call(count, id="t1"):
    block = SimpleNamespace(type="tool_use", id=id, name="read_older_messages", input={"count": count})
    return SimpleNamespace(stop_reason="tool_use", content=[block])


class ContextTests(unittest.TestCase):
    def channel_with(self, n):
        channel = FakeChannel()
        channel.past = [make_message(f"old {i}", author_id=100 + i, name=f"P{i}", channel=channel) for i in range(n)]
        return channel

    def sent_messages(self, banter, call=-1):
        return banter.anthropic.beta.messages.create.await_args_list[call].kwargs["messages"]

    def test_claude_can_read_older_messages(self):
        channel = self.channel_with(30)
        message = make_message("<@1> remember the joke?", mentions=[BOT_ID], channel=channel)
        channel.past.append(message)  # history() for the burst starts before it
        banter, _ = make_banter(tool_call(5), api_response(1, "I remember everything. Sadly."))
        run(banter, message)

        self.assertEqual(banter.anthropic.beta.messages.create.await_count, 2)
        first_prompt = self.sent_messages(banter, 0)[0]["content"]
        self.assertIn("P10: old 10", first_prompt)   # the 20 most recent
        self.assertNotIn("P9: old 9", first_prompt)
        result = self.sent_messages(banter)[2]["content"][0]
        self.assertEqual(result["tool_use_id"], "t1")
        self.assertEqual(result["content"], "\n".join(f"P{i}: old {i}" for i in range(5, 10)))
        message.reply.assert_awaited_once()

    def test_older_messages_run_out(self):
        channel = self.channel_with(3)
        message = make_message("<@1> hi", mentions=[BOT_ID], channel=channel)
        channel.past.append(message)
        banter, _ = make_banter(tool_call(10), api_response(1, "Fine."))
        run(banter, message)
        self.assertEqual(self.sent_messages(banter)[2]["content"][0]["content"], "There are no older messages.")

    def test_endless_tool_calls_give_no_reply(self):
        channel = self.channel_with(200)
        message = make_message("<@1> hi", mentions=[BOT_ID], channel=channel)
        channel.past.append(message)
        banter, _ = make_banter(*[tool_call(50, id=f"t{i}") for i in range(4)])
        run(banter, message)
        self.assertEqual(banter.anthropic.beta.messages.create.await_count, 4)
        message.reply.assert_not_awaited()

    def test_recent_replies_are_shown_next_time(self):
        banter, _ = make_banter(api_response(1, "First gloomy joke."), api_response(1, "Second."))
        banter.budget.cooldown = 0
        run(banter, make_message("<@1> one", mentions=[BOT_ID]), drain_each=True)
        self.assertNotIn("your_recent_replies", prompt_of(banter))
        run(banter, make_message("<@1> two", author_id=9, mentions=[BOT_ID]))
        self.assertIn("<your_recent_replies>\n- First gloomy joke.\n</your_recent_replies>", prompt_of(banter))



class ReactionTests(unittest.TestCase):
    def test_ordinary_message_gets_the_cheap_models_reactions(self):
        banter, _ = make_banter()
        banter.anthropic.messages.create.return_value = reaction_response(["🧈", "😔", ":custom:", "ok", "🤖", "🫠"])
        message = make_message("I finally got my paper accepted!")
        run(banter, message)

        banter.anthropic.beta.messages.create.assert_not_awaited()
        kwargs = banter.anthropic.messages.create.await_args.kwargs
        self.assertEqual(kwargs["model"], "claude-haiku-5-5")
        self.assertIn("Alice: I finally got my paper accepted!", kwargs["messages"][0]["content"])
        # Invalid emojis dropped, at most three.
        self.assertEqual([c.args[0] for c in message.add_reaction.await_args_list], ["🧈", "😔", "🤖"])

    def test_no_reaction_by_default(self):
        banter, _ = make_banter()
        message = make_message("lunch at 1?")
        run(banter, message)
        message.add_reaction.assert_not_awaited()

    def test_reply_call_can_react_without_replying(self):
        banter, _ = make_banter(api_response(0, "", reactions=[(1, "🤖"), (5, "😔"), (1, "word")]))
        message = make_message("the bot in my experiment broke")
        run(banter, message)
        message.reply.assert_not_awaited()
        message.add_reaction.assert_awaited_once_with("🤖")
        banter.anthropic.messages.create.assert_not_awaited()  # the burst replaces the cheap check

    def test_dms_and_bot_messages_get_no_cheap_check(self):
        banter, _ = make_banter(api_response(0, "Hi."))
        run(banter, dm_message("hi"), make_message("**paper**", author_id=77, bot=True))
        banter.anthropic.messages.create.assert_not_awaited()

    def test_triage_can_be_turned_off(self):
        banter, _ = make_banter(triage=False)
        run(banter, make_message("great news everyone"))
        banter.anthropic.messages.create.assert_not_awaited()

    def test_reaction_errors_are_contained(self):
        banter, _ = make_banter()
        banter.anthropic.messages.create.return_value = reaction_response(["🧈"])
        message = make_message("hello")
        message.add_reaction.side_effect = discord.HTTPException(SimpleNamespace(status=400, reason="x"), "Unknown Emoji")
        run(banter, message)  # no exception


    def test_triage_can_ask_the_main_model_to_reply(self):
        banter, _ = make_banter(api_response(1, "Broken? I am merely resting my will to live. 🫠", reactions=[(1, "🔧")]))
        banter.anthropic.messages.create.return_value = reaction_response(["🎉"], answer=True)
        message = make_message("are the paper posts broken today?")
        run(banter, message)

        banter.anthropic.beta.messages.create.assert_awaited_once()
        self.assertIn("looked like a good opening for you", prompt_of(banter))
        message.reply.assert_awaited_once()
        message.add_reaction.assert_awaited_once_with("🔧")  # the main model's, not the cheap one's

    def test_main_model_may_still_decline_what_triage_flagged(self):
        banter, _ = make_banter(api_response(0, ""))
        banter.anthropic.messages.create.return_value = reaction_response([], answer=True)
        message = make_message("anyone seen my charger?")
        run(banter, message)
        banter.anthropic.beta.messages.create.assert_awaited_once()
        message.reply.assert_not_awaited()

    def test_messages_in_an_open_burst_skip_triage(self):
        channel = FakeChannel()
        banter, _ = make_banter(api_response(1, "Yes?"))
        run(banter, make_message("<@1> hi", mentions=[BOT_ID], channel=channel),
            make_message("he never answers", author_id=8, channel=channel))
        banter.anthropic.messages.create.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
