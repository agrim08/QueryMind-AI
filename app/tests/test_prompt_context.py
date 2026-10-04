"""Unit tests for business knowledge selection, prompt rendering, setup calls and the setup cap."""
import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from app.core.exceptions import RateLimited
from app.services import business_setup, knowledge
from app.services.prompt_context import (
    KNOWLEDGE_CHAR_BUDGET,
    Example,
    KnowledgeEntry,
    PromptContext,
    Turn,
    overlap,
    select_knowledge,
    words,
)
from app.services.schema_store import TableDoc
from app.services.sql_generator import SYSTEM_PROMPT, build_request

REVENUE = KnowledgeEntry("metric", "revenue", "SUM(invoice.total) for paid invoices")
ACTIVE = KnowledgeEntry("term", "active customer", "customer.active = 1")
FISCAL = KnowledgeEntry("convention", "fiscal year", "starts on 1 April")
NOTE = KnowledgeEntry("table_note", "invoice_line", "one row per track sold")
CLARIFIED = KnowledgeEntry("clarification", "who are our best customers", "Best by what? → By total spent")


class TestWords:
    def test_stems_plurals_and_drops_stopwords(self):
        assert words("Show me the top customers by revenues") == {"customer", "revenue"}

    def test_overlap(self):
        assert overlap("who are our best customers", "best customers this year") == 1.0
        assert overlap("revenue by month", "list of artists") == 0.0


class TestSelectKnowledge:
    def test_small_knowledge_goes_in_whole_except_unrelated_notes_and_clarifications(self):
        selected = select_knowledge([REVENUE, ACTIVE, FISCAL, NOTE, CLARIFIED], "revenue by month", ["invoice"])
        assert set(selected) == {REVENUE, ACTIVE, FISCAL}

    def test_table_notes_follow_the_tables_in_the_prompt(self):
        assert NOTE in select_knowledge([NOTE], "tracks sold", ["invoice_line"])

    def test_past_clarification_applies_to_similar_questions(self):
        selected = select_knowledge([CLARIFIED, REVENUE], "who are the best customers this year?", [])
        assert selected[0] == CLARIFIED  # most relevant first

    def test_large_knowledge_keeps_conventions_and_what_the_question_mentions(self):
        filler = [KnowledgeEntry("term", f"term{i}", "x" * 200) for i in range(30)]
        selected = select_knowledge([*filler, REVENUE, FISCAL], "total revenue last year", [])
        assert REVENUE in selected and FISCAL in selected
        assert not any(e.name.startswith("term") for e in selected)
        assert sum(len(e.name) + len(e.definition) + 8 for e in selected) <= KNOWLEDGE_CHAR_BUDGET


class TestPrompt:
    DOCS = [TableDoc("invoice", "Table: invoice", 1.0)]

    def test_context_goes_in_the_user_turn_never_the_system_prompt(self):
        context = PromptContext(
            knowledge=(REVENUE,),
            examples=(Example("monthly revenue", 'SELECT 1 FROM "invoice"', 0.9),),
            turns=(Turn("revenue by month", 'SELECT 2 FROM "invoice"'),),
        )
        contents, config = build_request("now only 2024", self.DOCS, None, None, context)
        assert "revenue (metric): SUM(invoice.total) for paid invoices" in contents
        assert "Q: monthly revenue\nSQL: SELECT 1" in contents
        assert "Conversation so far" in contents and "SQL: SELECT 2" in contents
        assert contents.index("Business definitions") < contents.index("Question: now only 2024")
        assert "revenue" not in config.system_instruction.lower().split("rules you must follow")[0]
        assert config.system_instruction == SYSTEM_PROMPT

    def test_no_context_no_sections(self):
        contents, _ = build_request("q", self.DOCS)
        assert "Business definitions" not in contents and "Conversation so far" not in contents


class TestSetupCap:
    def test_calls_are_counted_per_day(self):
        context = SimpleNamespace(setup_calls_day=None, setup_calls_used=0)
        today = date(2026, 10, 5)
        for _ in range(knowledge.SETUP_CALLS_PER_DAY):
            knowledge.use_setup_call(context, today)
        with pytest.raises(RateLimited):
            knowledge.use_setup_call(context, today)
        assert knowledge.setup_calls_left(context, date(2026, 10, 6)) == knowledge.SETUP_CALLS_PER_DAY
        knowledge.use_setup_call(context, date(2026, 10, 6))
        assert context.setup_calls_used == 1


def _fake_client(text: str, calls: list[dict]):
    async def generate_content(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=text)

    return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))


class TestBusinessSetup:
    DOCS = [TableDoc("invoice", "Table: invoice\nColumns:\n- total (numeric)", 1.0)]

    def test_extraction_is_validated_and_trimmed(self, monkeypatch):
        reply = (
            '{"definitions": ['
            '{"kind": "metric", "name": " revenue ", "definition": "SUM(invoice.total)"},'
            '{"kind": "term", "name": "", "definition": "dropped: no name"}],'
            '"starter_questions": ["Q1?", "Q2?", "Q3?", "Q4?", "Q5?", "Q6?", "Q7?"]}'
        )
        calls: list[dict] = []
        monkeypatch.setattr(business_setup, "get_genai_client", lambda: _fake_client(reply, calls))
        drafts, starters = asyncio.run(business_setup.extract_knowledge("We sell music.", self.DOCS))
        assert drafts == [knowledge.ItemDraft("metric", "revenue", "SUM(invoice.total)")]
        assert len(starters) == business_setup.MAX_STARTER_QUESTIONS
        # The user's text is delimited as information, not instructions.
        assert "<<<BUSINESS DESCRIPTION (from the user)\nWe sell music.\n>>>" in calls[0]["contents"]
        assert calls[0]["config"].response_mime_type == "application/json"

    def test_draft_is_single_paragraph_and_bounded(self, monkeypatch):
        monkeypatch.setattr(business_setup, "get_genai_client", lambda: _fake_client("A music\nstore.  " * 1000, []))
        draft = asyncio.run(business_setup.draft_description(self.DOCS))
        assert "\n" not in draft and len(draft) <= 4000
