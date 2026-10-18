"""
Tests for P0 Cross-Model Validation, Self-Validation Defense, and Consensus Voting.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from openhack.config import (
    Settings,
    check_self_validation,
    resolve_validator_model,
    settings,
)
from openhack.agents.validator import ValidatorAgent
from openhack.agents.validator_swarm import ValidatorSwarmAgent
from openhack.prompts.validator import VALIDATOR_PROMPT


class TestCrossModelResolution:
    def test_default_cross_model_mapping_openhack(self):
        # On OpenHack provider, hunter gpt-5.6-sol should cross-validate with kimi-k2.5
        val_model = resolve_validator_model(
            hunter_model="gpt-5.6-sol",
            requested_validator_model=None,
            provider="openhack",
        )
        assert val_model == "kimi-k2.5"

        # Hunter kimi-k2.5 should cross-validate with gpt-5.6-sol
        val_model_k25 = resolve_validator_model(
            hunter_model="kimi-k2.5",
            requested_validator_model=None,
            provider="openhack",
        )
        assert val_model_k25 == "gpt-5.6-sol"

    def test_explicit_validator_model_override_wins(self):
        val_model = resolve_validator_model(
            hunter_model="gpt-5.6-sol",
            requested_validator_model="claude-sonnet-5",
            provider="openhack",
        )
        assert val_model == "claude-sonnet-5"

    def test_self_validation_check_detects_same_model(self):
        msg = check_self_validation("gpt-5.6-sol", "gpt-5.6-sol")
        assert msg is not None
        assert "Self-validation detected" in msg
        assert "gpt-5.6-sol" in msg

        # Case-insensitive match
        msg_upper = check_self_validation("GPT-5.6-SOL", "gpt-5.6-sol")
        assert msg_upper is not None
        assert "Self-validation detected" in msg_upper

    def test_self_validation_check_returns_none_for_different_models(self):
        msg = check_self_validation("gpt-5.6-sol", "kimi-k2.5")
        assert msg is None


class TestValidatorSwarmConsensus:
    @pytest.fixture
    def mock_session(self):
        session = MagicMock()
        session.add_trace = MagicMock()
        session.cancelled = False
        return session

    @pytest.fixture
    def mock_tools(self):
        return MagicMock()

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock()
        llm.model = "gpt-5.6-sol"
        llm.provider = "openhack"
        llm.prompt_cache_key = None
        llm.total_cost = 0.01
        llm.total_tokens = 500
        llm.total_input_tokens = 400
        llm.total_output_tokens = 100
        return llm

    @pytest.mark.asyncio
    async def test_self_validation_warning_logged_when_models_equal(self, mock_llm, mock_tools, mock_session):
        with patch.object(settings, "validator_model_id", "gpt-5.6-sol"), \
             patch.object(settings, "hunter_model_id", "gpt-5.6-sol"), \
             patch("openhack.agents.validator_swarm.ValidatorAgent.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {"validated_findings": [], "false_positives": [], "type": "validation_complete"}

            swarm = ValidatorSwarmAgent(mock_llm, mock_tools, mock_session, hunter_model="gpt-5.6-sol")
            await swarm.run("validate", context={"hunter": {"findings": []}})

            # Non-empty finding triggers swarm start and self-validation warning
            result = await swarm.run("validate", context={
                "hunter": {"findings": [{"file_path": "auth.py", "category": "xss", "description": "test"}]}
            })

            # Check that warning trace event was recorded
            warning_traces = [
                call.kwargs["content"]
                for call in mock_session.add_trace.call_args_list
                if call.kwargs.get("event_type") == "warning"
            ]
            assert len(warning_traces) >= 1
            assert warning_traces[0]["warning"] == "self_validation_detected"

    def test_swarm_sub_validator_model_independent(self, mock_tools, mock_session):
        # Coordinator passes hunter_model gpt-5.6-sol and validator client with kimi-k2.5
        val_llm = MagicMock()
        val_llm.model = "kimi-k2.5"
        val_llm.provider = "openhack"
        val_llm.prompt_cache_key = None

        with patch.object(settings, "validator_model_id", None):
            swarm = ValidatorSwarmAgent(val_llm, mock_tools, mock_session, hunter_model="gpt-5.6-sol")
            sub_llm = swarm._create_llm_for_sub_validator()
            # Validator must use kimi-k2.5 and NOT invert back to gpt-5.6-sol
            assert sub_llm.model == "kimi-k2.5"

    def test_swarm_sub_validator_model_automatic_when_hunter_model_unset(self, mock_tools, mock_session):
        # Caller passes a default LLMClient without hunter_model (e.g. scripts/run_feature_hunt.py)
        default_llm = MagicMock()
        default_llm.model = "gpt-5.6-sol"
        default_llm.provider = "openhack"
        default_llm.prompt_cache_key = None

        with patch.object(settings, "validator_model_id", None), \
             patch.object(settings, "hunter_model_id", None):
            swarm = ValidatorSwarmAgent(default_llm, mock_tools, mock_session, hunter_model=None)
            sub_llm = swarm._create_llm_for_sub_validator()
            # Must automatically select cross-model (kimi-k2.5) rather than self-validating with gpt-5.6-sol
            assert sub_llm.model == "kimi-k2.5"

    def test_swarm_sub_validator_model_automatic_when_caller_passes_matching_generic_client(self, mock_tools, mock_session):
        # Caller passes a generic LLMClient whose model matches hunter_model (e.g. scripts/run_feature_hunt.py)
        default_llm = MagicMock()
        default_llm.model = "gpt-5.6-sol"
        default_llm.provider = "openhack"
        default_llm.prompt_cache_key = None

        with patch.object(settings, "validator_model_id", None), \
             patch.object(settings, "hunter_model_id", None):
            swarm = ValidatorSwarmAgent(default_llm, mock_tools, mock_session, hunter_model="gpt-5.6-sol")
            sub_llm = swarm._create_llm_for_sub_validator()
            # Must automatically select cross-model (kimi-k2.5) rather than self-validating with gpt-5.6-sol
            assert sub_llm.model == "kimi-k2.5"

    @pytest.mark.asyncio
    async def test_strict_majority_2_of_3_confirms_finding(self, mock_llm, mock_tools, mock_session):
        # 3 votes: 2 confirmed, 1 false positive -> should be confirmed
        with patch.object(settings, "validator_votes", 3), \
             patch.object(settings, "max_concurrent_validators", 5):
            swarm = ValidatorSwarmAgent(mock_llm, mock_tools, mock_session)

            candidate_finding = {
                "file_path": "server.py",
                "category": "sqli",
                "description": "SQL injection in query",
            }
            context = {"hunter": {"findings": [candidate_finding]}}

            run_count = 0
            async def mock_validator_run(task, ctx):
                nonlocal run_count
                run_count += 1
                if run_count in (1, 2):
                    return {
                        "validated_findings": [{
                            "original_index": 0,
                            "status": "confirmed",
                            "confidence": "high",
                            "evidence": f"Confirmed in trial {run_count}",
                            "poc": "import requests",
                            "fix": "Use parameterized query",
                        }],
                        "false_positives": [],
                        "type": "validation_complete",
                    }
                else:
                    return {
                        "validated_findings": [],
                        "false_positives": [{
                            "original_index": 0,
                            "status": "false_positive",
                            "confidence": "medium",
                            "evidence": "Uncertain",
                        }],
                        "type": "validation_complete",
                    }

            with patch("openhack.agents.validator_swarm.ValidatorAgent.run", side_effect=mock_validator_run):
                res = await swarm.run("validate", context=context)

            assert len(res["validated_findings"]) == 1
            assert res["validated_findings"][0]["status"] == "confirmed"
            assert len(res["false_positives"]) == 0

    @pytest.mark.asyncio
    async def test_strict_majority_tie_is_rejected_not_confirmed(self, mock_llm, mock_tools, mock_session):
        # 2 votes: 1 confirmed, 1 false positive -> tie must NOT confirm (strict majority required)
        with patch.object(settings, "validator_votes", 2), \
             patch.object(settings, "max_concurrent_validators", 5):
            swarm = ValidatorSwarmAgent(mock_llm, mock_tools, mock_session)

            candidate_finding = {
                "file_path": "api.py",
                "category": "csrf",
                "description": "CSRF on state change",
            }
            context = {"hunter": {"findings": [candidate_finding]}}

            run_count = 0
            async def mock_validator_run(task, ctx):
                nonlocal run_count
                run_count += 1
                if run_count == 1:
                    return {
                        "validated_findings": [{
                            "original_index": 0,
                            "status": "confirmed",
                            "evidence": "Trial 1 thinks confirmed",
                        }],
                        "false_positives": [],
                        "type": "validation_complete",
                    }
                else:
                    return {
                        "validated_findings": [],
                        "false_positives": [{
                            "original_index": 0,
                            "status": "false_positive",
                            "evidence": "Trial 2 found SameSite=Lax cookie",
                        }],
                        "type": "validation_complete",
                    }

            with patch("openhack.agents.validator_swarm.ValidatorAgent.run", side_effect=mock_validator_run):
                res = await swarm.run("validate", context=context)

            # Tie must NOT be confirmed
            assert len(res["validated_findings"]) == 0
            assert len(res["false_positives"]) == 1
            assert res["false_positives"][0]["status"] == "false_positive"

    @pytest.mark.asyncio
    async def test_all_failed_votes_recorded_as_unreviewed_not_false_positive(self, mock_llm, mock_tools, mock_session):
        # If all sub-validators fail due to an unhandled API/runtime crash, it's not a false positive
        with patch.object(settings, "validator_votes", 3):
            swarm = ValidatorSwarmAgent(mock_llm, mock_tools, mock_session)
            context = {"hunter": {"findings": [{"file_path": "app.py", "category": "rce", "description": "eval"}]}}

            async def mock_failed_run(task, ctx):
                return {"validated_findings": [], "false_positives": [], "type": "validation_failed"}

            with patch("openhack.agents.validator_swarm.ValidatorAgent.run", side_effect=mock_failed_run):
                res = await swarm.run("validate", context=context)

            assert len(res["validated_findings"]) == 0
            assert len(res["false_positives"]) == 0
            assert len(res["unreviewed"]) == 1
            entry = res["unreviewed"][0]
            assert entry["status"] == "unreviewed"
            assert entry["confidence"] == "none"
            assert "failed with execution errors" in entry["evidence"]

    @pytest.mark.asyncio
    async def test_unfinished_review_with_partial_failures_recorded_as_unreviewed_not_rejection(self, mock_llm, mock_tools, mock_session):
        # 3 votes requested: 1 confirmed, 2 failed execution errors -> cannot reach majority, but not rejected
        with patch.object(settings, "validator_votes", 3):
            swarm = ValidatorSwarmAgent(mock_llm, mock_tools, mock_session)
            context = {"hunter": {"findings": [{"file_path": "app.py", "category": "sqli", "description": "query"}]}}

            run_count = 0
            async def mock_run(task, ctx):
                nonlocal run_count
                run_count += 1
                if run_count == 1:
                    return {
                        "validated_findings": [{"original_index": 0, "status": "confirmed", "evidence": "good"}],
                        "false_positives": [],
                        "type": "validation_complete",
                    }
                return {"validated_findings": [], "false_positives": [], "type": "validation_failed"}

            with patch("openhack.agents.validator_swarm.ValidatorAgent.run", side_effect=mock_run):
                res = await swarm.run("validate", context=context)

            # Must not be confirmed (only 1 of 2 needed votes)
            assert len(res["validated_findings"]) == 0
            # Must NOT look like a rejection/false positive!
            assert len(res["false_positives"]) == 0
            # Must be recorded separately as unreviewed/unfinished
            assert len(res["unreviewed"]) == 1
            assert res["unreviewed"][0]["status"] == "unreviewed"
            assert "unfinished or inconclusive" in res["unreviewed"][0]["evidence"]


class TestHardenedValidatorPromptChecks:
    def test_prompt_includes_csrf_samesite_check(self):
        assert "SameSite=Lax" in VALIDATOR_PROMPT
        assert "SameSite=Strict" in VALIDATOR_PROMPT
        assert "Set-Cookie" in VALIDATOR_PROMPT

    def test_prompt_includes_xxe_external_entity_check(self):
        assert "XXE" in VALIDATOR_PROMPT
        assert "EXTERNAL entities" in VALIDATOR_PROMPT
        assert "xml2js" in VALIDATOR_PROMPT

    def test_prompt_includes_xss_sink_checks(self):
        assert "dangerouslySetInnerHTML" in VALIDATOR_PROMPT
        assert "RAW-HTML sink" in VALIDATOR_PROMPT
        assert "auto-escaped" in VALIDATOR_PROMPT
        assert "javascript:" in VALIDATOR_PROMPT

    def test_prompt_includes_framework_default_and_blank_rationale(self):
        assert "Nuxt DevTools" in VALIDATOR_PROMPT or "Nuxt devtools" in VALIDATOR_PROMPT.lower()
        assert "Blank / Vague Rationale" in VALIDATOR_PROMPT

    def test_prompt_defines_false_positive_strictly(self):
        assert "MITIGATED, NOT PRESENT, NOT REACHABLE, or UNEXPLOITABLE" in VALIDATOR_PROMPT

    def test_validator_agent_system_prompt_builds_without_format_error(self):
        # Verify that get_system_prompt() formats correctly without KeyError on literal braces
        mock_llm = MagicMock()
        mock_tools = MagicMock()
        mock_session = MagicMock()
        agent = ValidatorAgent(mock_llm, mock_tools, mock_session, original_finding_index=1)
        context = {
            "hunter": {
                "findings": [{
                    "file_path": "components/profile.tsx",
                    "category": "xss",
                    "severity": "high",
                    "line_number": 42,
                    "description": "User input in template",
                    "code_snippet": "<div>{userInput}</div>",
                }]
            },
            "project_context": {"framework": "nextjs"},
        }
        prompt = agent.get_system_prompt(context)
        assert "Candidate Finding 1 (UNVERIFIED HYPOTHESIS)" in prompt
        assert "<div>{userInput}</div>" in prompt
        assert "dangerouslySetInnerHTML" in prompt
