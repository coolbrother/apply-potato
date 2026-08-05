"""
AI eligibility judgment.

Asks the concrete question — can this applicant apply for this role? — instead of
asking the model to encode the requirement into bounds that code then interprets.
That encoding is what repeatedly failed: minimum/maximum over one ordinal scale
cannot express "any Bachelor's student OR any grad student", and a floor and a
ceiling are structurally identical, so an inverted bound is a silently valid window.

Reads the scraped page directly rather than an ExtractedJob, since feeding it the
extracted fields would reintroduce the very encoding this exists to remove.

Every failure path returns None. None means "no judgment", and callers fall back to
filters.py — this module can decline to answer but must never manufacture a
rejection.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from google.api_core import exceptions as google_exceptions
from google.genai import types as genai_types
from openai import OpenAI, APIError, APITimeoutError, RateLimitError

from .config import Config, UserProfile, get_config

logger = logging.getLogger(__name__)


# The dimensions this pass owns. job_type and season_year stay in filters.py: they are
# unambiguous comparisons over well-formed fields and they already work.
JUDGED_DIMENSIONS = ("class_standing", "graduation_timeline", "work_authorization")

# Whitespace differs freely between the scraped text and a quoted span, so evidence is
# compared with runs of whitespace collapsed.
_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WS.sub(" ", (text or "")).strip().lower()


@dataclass
class EligibilityCheck:
    """One dimension's verdict, with the sentence it rests on."""
    dimension: str
    passes: bool
    evidence: str = ""
    reasoning: str = ""
    # False when `evidence` was not found verbatim in the posting.
    evidence_verified: bool = True


@dataclass
class EligibilityJudgment:
    """
    The pass's answer for one posting.

    `usable` is the question callers actually care about: a judgment that failed
    validation is kept for the disagreement log but must not decide anything.
    """
    eligible: bool
    checks: List[EligibilityCheck] = field(default_factory=list)
    blocking_reason: Optional[str] = None
    usable: bool = True
    discarded_reason: Optional[str] = None
    raw_response: Optional[str] = None

    @property
    def failing_check(self) -> Optional[EligibilityCheck]:
        return next((c for c in self.checks if not c.passes), None)

    def reason(self) -> str:
        """One line explaining the verdict, for the sheet and the logs."""
        if self.eligible:
            return "AI eligibility: applicant qualifies"
        check = self.failing_check
        if self.blocking_reason:
            return f"AI eligibility: {self.blocking_reason}"
        if check:
            return f"AI eligibility: fails {check.dimension} — {check.reasoning}"
        return "AI eligibility: not eligible"

    def evidence_quote(self) -> str:
        """The quote behind a rejection, for the Notes column."""
        check = self.failing_check
        return check.evidence if check else ""


class EligibilityJudge:
    """Runs the eligibility prompt against the configured provider."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()
        self._prompt_template: Optional[str] = None
        self._openai_client: Optional[OpenAI] = None
        self._gemini_client = None

    # ------------------------------------------------------------------ prompt

    def _load_prompt(self) -> str:
        if self._prompt_template is None:
            path = self.config.prompts_dir / "eligibility_judgment.txt"
            if not path.exists():
                raise FileNotFoundError(f"Prompt template not found: {path}")
            self._prompt_template = path.read_text(encoding="utf-8")
        return self._prompt_template

    def _build_prompt(self, content: str, user: UserProfile) -> str:
        template = self._load_prompt()
        # str.format would choke on the JSON braces in the template, so the fields are
        # substituted by name instead.
        majors = getattr(user, "majors", None) or []
        if isinstance(majors, str):
            majors = [majors]
        values = {
            "{class_standing}": user.class_standing or "(not specified — treat as graduated)",
            "{graduation_date}": getattr(user, "graduation_date", "") or "(not specified)",
            "{degree_level}": getattr(user, "degree_level", "") or "(not specified)",
            "{major}": ", ".join(majors) if majors else "(not specified)",
            "{work_authorization}": getattr(user, "work_authorization", "") or "(not specified)",
            "{content}": content,
        }
        for token, value in values.items():
            template = template.replace(token, str(value))
        return template

    # ------------------------------------------------------------------ clients

    def _get_openai_client(self) -> OpenAI:
        if self._openai_client is None:
            if not self.config.openai_api_key:
                raise ValueError("OpenAI API key not configured")
            self._openai_client = OpenAI(api_key=self.config.openai_api_key)
        return self._openai_client

    def _get_gemini_client(self):
        if self._gemini_client is None:
            if not self.config.gemini_api_key:
                raise ValueError("Gemini API key not configured")
            from google import genai
            self._gemini_client = genai.Client(api_key=self.config.gemini_api_key)
        return self._gemini_client

    def _call_openai(self, prompt: str) -> Optional[str]:
        client = self._get_openai_client()
        params: Dict[str, Any] = {
            "model": self.config.openai_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        }
        if self.config.openai_max_tokens:
            params["max_tokens"] = self.config.openai_max_tokens
        response = client.chat.completions.create(**params)
        if response.choices and response.choices[0].message.content:
            return response.choices[0].message.content.strip()
        return None

    def _call_gemini(self, prompt: str) -> Optional[str]:
        client = self._get_gemini_client()
        params: Dict[str, Any] = {"temperature": 0.1}
        if self.config.gemini_max_output_tokens:
            params["max_output_tokens"] = self.config.gemini_max_output_tokens
        response = client.models.generate_content(
            model=self.config.gemini_model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(**params),
        )
        return response.text.strip() if response.text else None

    # ------------------------------------------------------------------ parsing

    @staticmethod
    def _parse(raw: str) -> Optional[Dict[str, Any]]:
        """Pull the JSON object out of a response that may be fenced or prefaced."""
        if not raw:
            return None
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                return None
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        return parsed if isinstance(parsed, dict) else None

    def _build_judgment(self, data: Dict[str, Any], content: str, raw: str) -> EligibilityJudgment:
        """
        Turn the parsed response into a judgment, validating every rejection.

        A rejection is only allowed to stand when its evidence is a real span of the
        posting. Without that check the pass could reject on an invented quote, which
        is precisely the failure mode it was built to remove — and unlike a wrong
        bound, an invented sentence is not visible in any field afterwards.
        """
        haystack = _normalize(content)
        checks: List[EligibilityCheck] = []

        for item in data.get("checks") or []:
            if not isinstance(item, dict):
                continue
            dimension = str(item.get("dimension") or "").strip()
            if dimension not in JUDGED_DIMENSIONS:
                logger.debug(f"Ignoring check for unjudged dimension {dimension!r}")
                continue
            evidence = str(item.get("evidence") or "").strip()
            passes = bool(item.get("passes"))
            verified = True
            if not passes:
                normalized = _normalize(evidence)
                verified = bool(normalized) and normalized in haystack
            checks.append(EligibilityCheck(
                dimension=dimension,
                passes=passes,
                evidence=evidence,
                reasoning=str(item.get("reasoning") or "").strip(),
                evidence_verified=verified,
            ))

        blocking = data.get("blocking_reason")
        judgment = EligibilityJudgment(
            eligible=bool(data.get("eligible")),
            checks=checks,
            blocking_reason=str(blocking).strip() if blocking else None,
            raw_response=raw,
        )

        # A "not eligible" answer has to name a failing check, and that check has to
        # quote the posting. Anything else is discarded rather than trusted.
        if not judgment.eligible:
            failing = [c for c in checks if not c.passes]
            if not failing:
                judgment.usable = False
                judgment.discarded_reason = "said not eligible but no check failed"
            elif not any(c.evidence_verified for c in failing):
                quotes = "; ".join(c.evidence[:60] for c in failing) or "(none)"
                judgment.usable = False
                judgment.discarded_reason = (
                    f"rejection evidence not found in the posting: {quotes}"
                )

        if not judgment.usable:
            logger.warning(f"Eligibility judgment discarded — {judgment.discarded_reason}")

        return judgment

    # ------------------------------------------------------------------ public

    def judge(self, content: str, user: Optional[UserProfile] = None) -> Optional[EligibilityJudgment]:
        """
        Judge one posting. Returns None when no judgment could be made.

        None is not a rejection: callers fall back to filters.py. Every failure here —
        no content, API error, unparseable response — must leave the job decidable by
        the path that already works.
        """
        if not content or not content.strip():
            logger.debug("Eligibility judge: no content to judge")
            return None

        user = user or self.config.user
        try:
            prompt = self._build_prompt(content, user)
        except FileNotFoundError as e:
            logger.error(f"Eligibility judge: {e}")
            return None

        raw: Optional[str] = None
        for attempt in range(self.config.max_retries):
            try:
                if self.config.ai_provider == "openai":
                    raw = self._call_openai(prompt)
                else:
                    raw = self._call_gemini(prompt)
                if raw:
                    break
            except (RateLimitError, google_exceptions.ResourceExhausted):
                wait = self.config.retry_base_delay_seconds * (2 ** attempt)
                logger.warning(f"Eligibility judge rate limited, waiting {wait}s")
                time.sleep(wait)
            except (APITimeoutError, google_exceptions.DeadlineExceeded):
                wait = self.config.retry_base_delay_seconds * (2 ** attempt)
                logger.warning(f"Eligibility judge timed out, waiting {wait}s")
                time.sleep(wait)
            except (APIError, google_exceptions.GoogleAPIError) as e:
                logger.error(f"Eligibility judge API error: {e}")
                break
            except Exception as e:
                logger.error(f"Eligibility judge unexpected error: {e}")
                break

        if not raw:
            logger.warning("Eligibility judge returned nothing")
            return None

        data = self._parse(raw)
        if data is None:
            logger.warning("Eligibility judge response was not valid JSON")
            return None

        return self._build_judgment(data, content, raw)


_judge: Optional[EligibilityJudge] = None


def get_eligibility_judge(config: Optional[Config] = None) -> EligibilityJudge:
    """Global EligibilityJudge, mirroring the other singletons in this package."""
    global _judge
    if _judge is None:
        _judge = EligibilityJudge(config)
    return _judge


def reset_eligibility_judge() -> None:
    """Reset the singleton (for tests)."""
    global _judge
    _judge = None
