"""
Validator swarm agent that spawns one sub-validator per finding.
"""

import asyncio
import logging
from typing import Optional

from .validator import ValidatorAgent
from .llm import LLMClient
from .session import Session
from openhack.tools.registry import ToolRegistry
from openhack.config import settings, resolve_validator_model, check_self_validation

logger = logging.getLogger(__name__)


class ValidatorSwarmAgent:
    name = "validator_swarm"
    description = "Validator swarm coordinator"

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        session: Session,
        hunter_model: Optional[str] = None,
    ):
        self.llm = llm
        self.tools = tools
        self.session = session
        self.hunter_model = hunter_model
        self.total_cost: float = 0.0
        self.total_tokens: int = 0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0

    def _get_models(self) -> tuple[str, str]:
        hunter_model = self.hunter_model or getattr(settings, "hunter_model_id", None) or self.llm.model
        if settings.validator_model_id:
            validator_model = settings.validator_model_id
        elif self.llm.model and self.llm.model != hunter_model:
            # Caller passed an already-resolved validator client distinct from the hunter model
            validator_model = self.llm.model
        else:
            # Caller passed a generic/default client without resolving an independent validator model;
            # preserve automatic cross-model selection so hunter does not review its own findings
            validator_model = resolve_validator_model(
                hunter_model=hunter_model,
                requested_validator_model=None,
                provider=self.llm.provider,
            )
        return hunter_model, validator_model

    def _create_llm_for_sub_validator(self) -> LLMClient:
        _, validator_model = self._get_models()
        return LLMClient(
            model=validator_model,
            temperature=0.0,
            max_tokens=8192,
            provider=self.llm.provider,
            prompt_cache_key=self.llm.prompt_cache_key,
        )

    def _build_sub_context(self, finding: dict, full_context: dict) -> dict:
        return {
            "hunter": {"findings": [finding]},
            "recon": full_context.get("recon", {}),
            "project_context": full_context.get("project_context", {}),
        }

    async def run(self, task: str, context: Optional[dict] = None) -> dict:
        context = context or {}
        findings = context.get("hunter", {}).get("findings", [])

        if not findings:
            return {
                "raw_output": "No findings to validate",
                "validated_findings": [],
                "false_positives": [],
                "unreviewed": [],
                "type": "validation_complete",
            }

        hunter_model, validator_model = self._get_models()

        # Startup check: warn if validator uses the same model as hunter (self-validation)
        self_val_warning = check_self_validation(hunter_model, validator_model)
        if self_val_warning:
            logger.warning(self_val_warning)
            self.session.add_trace(
                agent=self.name,
                event_type="warning",
                content={
                    "warning": "self_validation_detected",
                    "hunter_model": hunter_model,
                    "validator_model": validator_model,
                    "message": self_val_warning,
                },
            )

        votes = max(1, getattr(settings, "validator_votes", 1))
        self.session.add_trace(
            agent=self.name,
            event_type="swarm_start",
            content={
                "findings_count": len(findings),
                "hunter_model": hunter_model,
                "validator_model": validator_model,
                "validator_votes": votes,
            },
        )

        sub_validators: list[tuple[int, int, ValidatorAgent, dict]] = []
        for idx, finding in enumerate(findings):
            sub_context = self._build_sub_context(finding, context)
            for vote_idx in range(votes):
                llm = self._create_llm_for_sub_validator()
                validator = ValidatorAgent(llm, self.tools, self.session, original_finding_index=idx)
                sub_validators.append((idx, vote_idx, validator, sub_context))

        semaphore = asyncio.Semaphore(settings.max_concurrent_validators)

        async def run_sub_validator(finding_idx, vote_idx, validator, sub_context):
            async with semaphore:
                try:
                    sub_task = (
                        "Validate this potential vulnerability. Confirm whether it is real, "
                        "generate a PoC, and suggest a fix."
                    )
                    result = await validator.run(sub_task, sub_context)
                    return finding_idx, vote_idx, result
                except Exception as e:
                    logger.error(f"Sub-validator for finding {finding_idx} (vote {vote_idx}) failed: {e}")
                    return finding_idx, vote_idx, {"validated_findings": [], "false_positives": [], "type": "validation_failed"}

        tasks = [
            asyncio.create_task(run_sub_validator(idx, vote_idx, validator, sub_ctx))
            for idx, vote_idx, validator, sub_ctx in sub_validators
        ]
        try:
            results = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        results_by_finding: dict[int, list[dict]] = {idx: [] for idx in range(len(findings))}
        for finding_idx, vote_idx, result in results:
            results_by_finding[finding_idx].append(result)

        all_validated: list[dict] = []
        all_false_positives: list[dict] = []
        all_unreviewed: list[dict] = []
        threshold = (votes // 2) + 1

        for finding_idx, run_results in results_by_finding.items():
            confirmed_runs = [
                r for r in run_results if r.get("validated_findings")
            ]
            confirmed_count = len(confirmed_runs)
            rejection_runs = [
                r for r in run_results if r.get("false_positives")
            ]
            failed_runs = [
                r for r in run_results if r.get("type") == "validation_failed"
            ]

            # Strict majority required (>= threshold). Ties are NOT confirmed.
            if confirmed_count >= threshold and confirmed_runs:
                best_run = max(
                    confirmed_runs,
                    key=lambda r: len(r["validated_findings"][0].get("evidence", ""))
                    if r["validated_findings"]
                    else 0,
                )
                all_validated.extend(best_run.get("validated_findings", []))
                self.session.add_trace(
                    agent=self.name,
                    event_type="validation_consensus",
                    content={
                        "finding_index": finding_idx,
                        "status": "confirmed",
                        "confirmed_votes": confirmed_count,
                        "total_votes": votes,
                        "threshold": threshold,
                    },
                )
            elif (len(failed_runs) > 0 and len(rejection_runs) < threshold) or not run_results:
                # Review is unfinished or inconclusive because one or more votes failed execution
                # (API error, prompt error, or timeout) without achieving a definitive rejection.
                # Record unfinished/failed review separately from an actual rejection.
                unreviewed_entry = {
                    "original_index": finding_idx,
                    "status": "unreviewed",
                    "confidence": "none",
                    "evidence": (
                        f"Validation review was unfinished or inconclusive ({confirmed_count} confirmed, "
                        f"{len(rejection_runs)} rejected, {len(failed_runs)} failed with execution errors across {votes} attempts)."
                    ),
                }
                all_unreviewed.append(unreviewed_entry)
                self.session.add_trace(
                    agent=self.name,
                    event_type="validation_consensus",
                    content={
                        "finding_index": finding_idx,
                        "status": "unreviewed",
                        "confirmed_votes": confirmed_count,
                        "rejected_votes": len(rejection_runs),
                        "failed_votes": len(failed_runs),
                        "total_votes": votes,
                        "threshold": threshold,
                        "review_failed": True,
                    },
                )
            else:
                fp_entry = None
                for r in run_results:
                    if r.get("false_positives"):
                        fp_entry = r["false_positives"][0]
                        break
                if not fp_entry:
                    fp_entry = {
                        "original_index": finding_idx,
                        "status": "false_positive",
                        "confidence": "medium",
                        "evidence": f"Failed strict majority validation ({confirmed_count}/{votes} confirmed votes, required >={threshold})",
                    }
                all_false_positives.append(fp_entry)
                self.session.add_trace(
                    agent=self.name,
                    event_type="validation_consensus",
                    content={
                        "finding_index": finding_idx,
                        "status": fp_entry.get("status", "false_positive"),
                        "confirmed_votes": confirmed_count,
                        "total_votes": votes,
                        "threshold": threshold,
                        "review_failed": False,
                    },
                )

        for _, _, validator, _ in sub_validators:
            self.total_cost += validator.llm.total_cost
            self.total_tokens += validator.llm.total_tokens
            self.total_input_tokens += validator.llm.total_input_tokens
            self.total_output_tokens += validator.llm.total_output_tokens

        self.session.add_trace(
            agent=self.name,
            event_type="swarm_complete",
            content={
                "total_confirmed": len(all_validated),
                "total_false_positives": len(all_false_positives),
                "total_unreviewed": len(all_unreviewed),
                "total_cost": self.total_cost,
                "total_tokens": self.total_tokens,
                "validator_votes": votes,
            },
        )

        return {
            "raw_output": f"Validated {len(findings)} findings: {len(all_validated)} confirmed, {len(all_false_positives)} false positives, {len(all_unreviewed)} unreviewed",
            "validated_findings": all_validated,
            "false_positives": all_false_positives,
            "unreviewed": all_unreviewed,
            "type": "validation_complete",
        }
