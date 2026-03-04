"""
Unit tests for pure logic functions in memory_wrapper.py.
No database, no Ollama, no API calls required.
"""

import json
import sys
import os
import pytest

# Add shared/ to path so we can import memory_wrapper functions
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))

# We import individual functions directly to avoid triggering DB connections
# at module load time
from memory_wrapper import (
    _is_platitude,
    _extract_from_jsonl,
    AGENT_SCHEMAS,
    AGENT_DISPLAY_NAMES,
    KV_WAKE_RETRIEVED,
)


# ============================================================
# _is_platitude — filters generic filler memories
# ============================================================

class TestIsPlatitude:

    def test_rejects_importance_of(self):
        assert _is_platitude("the importance of clear communication in debates") is True

    def test_rejects_necessity_of(self):
        assert _is_platitude("the necessity of rigorous data validation") is True

    def test_rejects_value_of(self):
        assert _is_platitude("the value of strategic thinking") is True

    def test_rejects_need_to(self):
        assert _is_platitude("the need to stay focused on long-term goals") is True

    def test_allows_platitude_with_specific_data(self):
        # Contains a number → specific, not a platitude
        assert _is_platitude("the importance of the 43000 restart crash loop fix") is False

    def test_allows_platitude_with_url(self):
        assert _is_platitude("the value of https://clawbr.org for debate engagement") is False

    def test_allows_platitude_with_mention(self):
        assert _is_platitude("the need to challenge @maxanvil on crypto positions") is False

    def test_passes_normal_memory(self):
        assert _is_platitude("Neo4j Phase 0 sync complete: 2320 nodes, 6528 edges") is False

    def test_passes_short_specific(self):
        assert _is_platitude("BTC broke 100k resistance, expect consolidation") is False

    def test_case_insensitive(self):
        assert _is_platitude("The Importance Of long form debate strategy") is True


# ============================================================
# _extract_from_jsonl — parses Claude Code stream output
# ============================================================

class TestExtractFromJsonl:

    def _make_jsonl(self, entries):
        return '\n'.join(json.dumps(e) for e in entries)

    def test_extracts_assistant_text(self):
        content = self._make_jsonl([
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello there"}]}}
        ])
        from memory_wrapper import _extract_from_jsonl
        result = _extract_from_jsonl(content)
        assert "[ASSISTANT] Hello there" in result

    def test_extracts_user_text(self):
        content = self._make_jsonl([
            {"type": "human", "message": {"content": [{"type": "text", "text": "What is EVA?"}]}}
        ])
        result = _extract_from_jsonl(content)
        assert "[USER] What is EVA?" in result

    def test_skips_system_reminders(self):
        content = self._make_jsonl([
            {"type": "human", "message": {"content": [{"type": "text", "text": "<system-reminder>some injected stuff</system-reminder>"}]}}
        ])
        result = _extract_from_jsonl(content)
        assert "system-reminder" not in result

    def test_skips_invalid_json_lines(self):
        content = "not json\n" + json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "valid"}]}})
        result = _extract_from_jsonl(content)
        assert "[ASSISTANT] valid" in result

    def test_skips_empty_lines(self):
        content = "\n\n" + json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "clean"}]}})
        result = _extract_from_jsonl(content)
        assert "[ASSISTANT] clean" in result

    def test_truncates_long_content(self):
        long_text = "x" * 100
        entries = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": long_text}]}}
            for _ in range(200)
        ]
        content = self._make_jsonl(entries)
        result = _extract_from_jsonl(content, max_chars=500)
        assert len(result) <= 1000  # truncated with markers, not unbounded
        assert "[...]" in result


# ============================================================
# Agent config constants — sanity checks
# ============================================================

class TestAgentConfig:

    def test_all_agents_have_schemas(self):
        expected = {'max', 'beth', 'susan', 'debater', 'gerald'}
        assert set(AGENT_SCHEMAS.keys()) == expected

    def test_all_agents_have_display_names(self):
        expected = {'max', 'beth', 'susan', 'debater', 'gerald'}
        assert set(AGENT_DISPLAY_NAMES.keys()) == expected

    def test_schema_values_are_strings(self):
        for agent, schema in AGENT_SCHEMAS.items():
            assert isinstance(schema, str), f"{agent} schema is not a string"

    def test_display_names_are_non_empty(self):
        for agent, name in AGENT_DISPLAY_NAMES.items():
            assert name and len(name) > 0, f"{agent} has empty display name"

    def test_kv_wake_key_format(self):
        assert KV_WAKE_RETRIEVED.startswith('.')
        assert 'wake' in KV_WAKE_RETRIEVED
