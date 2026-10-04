"""Unit tests for the model reply format: parsing and the live SQL stream filter."""
import random

import pytest

from app.services.reply_format import Clarification, SqlReply, SqlStreamFilter, parse_reply
from app.services.sql_generator import SYSTEM_PROMPT

FULL_REPLY = "\n".join([
    "-- Intent: ranking",
    "-- Understood: Top 5 customers by total spent",
    "-- Assumption: spent means the sum of invoice totals",
    'SELECT "c"."email", SUM("i"."total") AS "spent"',
    '  -- totals per customer',
    'FROM "customer" AS "c" JOIN "invoice" AS "i" USING ("customer_id")',
    'GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT 5',
    "-- Instead: Top 5 customers by number of invoices",
    "-- Follow-up: How has total spending changed by month?",
    "-- Follow-up: Which countries spend the most?",
])


class TestParseReply:
    def test_full_sql_reply(self):
        reply = parse_reply(FULL_REPLY)
        assert reply.intent == "ranking"
        assert reply.understood == "Top 5 customers by total spent"
        assert reply.assumptions == ("spent means the sum of invoice totals",)
        assert reply.alternatives == ("Top 5 customers by number of invoices",)
        assert reply.follow_ups == ("How has total spending changed by month?", "Which countries spend the most?")
        # Only the statement is validated and executed; a comment inside it stays.
        assert reply.sql.startswith('SELECT "c"."email"')
        assert reply.sql.endswith("LIMIT 5")
        assert "-- totals per customer" in reply.sql
        assert "Follow-up" not in reply.sql and "Intent" not in reply.sql

    def test_plain_sql_without_header(self):
        assert parse_reply("  SELECT 1;  ") == SqlReply(sql="SELECT 1;")

    @pytest.mark.parametrize(
        "text",
        ["```sql\nSELECT 1\n```", "```\nSELECT 1\n```", "```postgresql\nSELECT 1```", "Here it is:\n```sql\nSELECT 1\n```"],
    )
    def test_markdown_fences_are_stripped(self, text):
        assert parse_reply(text).sql == "SELECT 1"

    def test_clarify(self):
        reply = parse_reply(
            "-- Intent: ranking\n-- Understood: Best customers\n-- Clarify: Best by what?\n"
            "-- Option: By total amount spent (invoice.total)\n-- Option: By number of orders"
        )
        assert reply.sql == ""
        assert reply.clarify == Clarification(
            "Best by what?", ("By total amount spent (invoice.total)", "By number of orders")
        )

    def test_clarify_without_options_falls_back_to_no_answer(self):
        reply = parse_reply("-- Clarify: Best by what?")
        assert reply.clarify is None and reply.sql == ""

    def test_schema_answer(self):
        reply = parse_reply("-- Intent: schema\n-- Answer: Emails are in customer.email.\n-- Answer: Staff emails are in staff.email.")
        assert reply.intent == "schema"
        assert reply.answer == "Emails are in customer.email. Staff emails are in staff.email."

    def test_not_allowed(self):
        assert parse_reply("-- Intent: other\n-- Not allowed: delete inactive users").not_allowed == "delete inactive users"

    @pytest.mark.parametrize(
        "text", ["-- Cannot answer: artists have no phone column", "--cannot answer artists have no phone column"]
    )
    def test_cannot_answer(self, text):
        reply = parse_reply(text)
        assert reply.sql == ""
        assert reply.cannot_answer == "artists have no phone column"

    def test_unknown_intent_becomes_other(self):
        assert parse_reply("-- Intent: forecast\nSELECT 1").intent == "other"

    def test_limits_on_repeated_lines(self):
        reply = parse_reply("SELECT 1\n" + "\n".join(f"-- Follow-up: q{i}?" for i in range(6)))
        assert len(reply.follow_ups) == 3

    def test_long_lines_are_trimmed(self):
        assert len(parse_reply("-- Understood: " + "x" * 1000 + "\nSELECT 1").understood) == 300


class TestSqlStreamFilter:
    @pytest.mark.parametrize(
        "reply",
        [
            FULL_REPLY,
            "SELECT 1",
            "-- Intent: number\nSELECT count(*) FROM t -- all rows",
            "-- Clarify: Best by what?\n-- Option: By total\n-- Option: By count",
            "-- Cannot answer: no such data",
        ],
    )
    def test_streamed_text_is_exactly_the_statement_however_it_is_chunked(self, reply):
        rng = random.Random(42)
        for _ in range(200):
            cuts = sorted(rng.sample(range(1, len(reply)), min(len(reply) - 1, rng.randint(0, 25))))
            parts = [reply[i:j] for i, j in zip([0, *cuts], [*cuts, len(reply)])]
            stream = SqlStreamFilter()
            shown = "".join(stream.feed(p) for p in parts) + stream.finish()
            assert shown.strip() == parse_reply(reply).sql

    def test_sql_streams_before_the_line_ends(self):
        stream = SqlStreamFilter()
        assert stream.feed("-- Intent: number\n") == ""
        assert stream.feed("SEL") == "SEL"
        assert stream.feed("ECT 1") == "ECT 1"


class TestPromptDescribesTheFormat:
    @pytest.mark.parametrize(
        "marker", ["-- Intent:", "-- Understood:", "-- Assumption:", "-- Instead:", "-- Follow-up:",
                   "-- Clarify:", "-- Option:", "-- Answer:", "-- Not allowed:", "-- Cannot answer:"],
    )
    def test_every_parsed_line_is_in_the_prompt(self, marker):
        assert marker in SYSTEM_PROMPT
