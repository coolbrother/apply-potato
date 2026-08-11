"""
AI-powered email classification for ApplyPotato.
Classifies job-related emails into categories (confirmation, OA, interview, offer, rejection).
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup
from openai import OpenAI, APIError, RateLimitError, APITimeoutError
from google import genai
from google.genai import types as genai_types
from google.api_core import exceptions as google_exceptions

from .config import Config, get_config
from .sheets import COMPLETABLE_STAGES
from .gmail import EmailMessage


logger = logging.getLogger(__name__)


# A bare host name or URL, with no whitespace and at least one dot-separated label in
# front of a TLD: "una-arcticshores.com", "https://greenhouse.io/x". Deliberately not
# anchored on a TLD list — anything shaped like a host is shaped like a host.
_HOSTNAME_RE = re.compile(
    r"^(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?$",
    re.IGNORECASE,
)


def drop_hostname_candidates(candidates: List[str]) -> List[str]:
    """
    Remove company candidates that are really the sending vendor's domain.

    Assessment platforms mail on the employer's behalf, so their host names are all over
    the body, and the model hands them back beside the real employer: a Maven Securities
    invite yielded "UNA" and "una-arcticshores.com" from the assessment host. Those get
    tried against the sheet like any other name, and a needless candidate is a needless
    chance to match the wrong row.

    The shape matched is a whole bare host, not any dotted string, so "St. Jude Medical"
    is never at risk — a host has no spaces. A name that genuinely is host-shaped, like
    "Amazon.com", does get dropped, which costs nothing while the model also returns
    "Amazon" and costs nothing when it does not: if filtering would empty the list, the
    original is kept. No candidates means no match at all, which is strictly worse than a
    candidate that probably will not match.
    """
    kept = [c for c in candidates if not _HOSTNAME_RE.match((c or "").strip())]
    if not kept:
        return candidates
    if len(kept) != len(candidates):
        dropped = [c for c in candidates if c not in kept]
        logger.debug(f"Dropped hostname company candidates: {dropped}")
    return kept


@dataclass
class EmailClassification:
    """Result of email classification."""
    category: str  # confirmation, oa, phone, technical, stage_done, offer, rejection, unknown
    confidence: float
    company_candidates: list  # List of company names found in email
    # Which stage the applicant finished. Only set for category "stage_done"; None
    # elsewhere, so a caller can treat its presence as the signal.
    stage_completed: Optional[str] = None
    position: Optional[str] = None
    date_mentioned: Optional[str] = None
    time_mentioned: Optional[str] = None
    action_required: Optional[str] = None
    key_details: Optional[str] = None
    note: Optional[str] = None  # short label for the sheet's Notes column


class EmailClassifier:
    """
    Classifies job-related emails using AI.

    Supports OpenAI and Gemini, with retry logic and JSON validation.
    """

    def __init__(self, config: Optional[Config] = None):
        """
        Initialize the email classifier.

        Args:
            config: Optional config object. Uses global config if not provided.
        """
        self.config = config or get_config()
        self._prompt_template: Optional[str] = None
        self._row_match_prompt: Optional[str] = None
        self._openai_client: Optional[OpenAI] = None
        self._gemini_client = None

    @property
    def prompt_template(self) -> str:
        """Load and cache the classification prompt template."""
        if self._prompt_template is None:
            prompt_path = self.config.prompts_dir / "email_classification.txt"
            if not prompt_path.exists():
                raise FileNotFoundError(f"Prompt template not found: {prompt_path}")
            self._prompt_template = prompt_path.read_text(encoding="utf-8")
            logger.debug(f"Loaded prompt template from {prompt_path}")
        return self._prompt_template

    def _get_openai_client(self) -> OpenAI:
        """Get or create OpenAI client."""
        if self._openai_client is None:
            if not self.config.openai_api_key:
                raise ValueError("OpenAI API key not configured")
            self._openai_client = OpenAI(api_key=self.config.openai_api_key)
        return self._openai_client

    def _get_gemini_client(self):
        """Get or create Gemini client."""
        if self._gemini_client is None:
            if not self.config.gemini_api_key:
                raise ValueError("Gemini API key not configured")
            self._gemini_client = genai.Client(api_key=self.config.gemini_api_key)
        return self._gemini_client

    def classify(self, email: EmailMessage) -> Optional[EmailClassification]:
        """
        Classify an email message.

        Args:
            email: EmailMessage object to classify

        Returns:
            EmailClassification if successful, None if classification failed
        """
        # Get body - prefer plain text, convert HTML if needed
        body = email.body_text
        if not body and email.body_html:
            soup = BeautifulSoup(email.body_html, 'html.parser')
            for element in soup(['script', 'style']):
                element.decompose()
            body = soup.get_text(separator='\n', strip=True)
            logger.debug(f"Converted HTML to plain text ({len(body)} chars)")

        if not body:
            logger.warning(f"Email has no body content: {email.subject}")
            return None

        # Build the prompt
        prompt = self.prompt_template
        prompt = prompt.replace("{subject}", email.subject)
        prompt = prompt.replace("{sender}", f"{email.sender} <{email.sender_email}>")
        prompt = prompt.replace("{date}", email.date.strftime("%Y-%m-%d %H:%M"))
        prompt = prompt.replace("{body}", body)

        # Call the appropriate AI provider
        if self.config.ai_provider == "openai":
            model_name = self.config.openai_model
        else:
            model_name = self.config.gemini_model
        logger.info(f"Classifying email using {self.config.ai_provider} ({model_name})")

        raw_response: Optional[str] = None

        for attempt in range(self.config.max_retries):
            try:
                if self.config.ai_provider == "openai":
                    raw_response = self._classify_openai(prompt)
                else:
                    raw_response = self._classify_gemini(prompt)

                if raw_response:
                    break

            except (RateLimitError, google_exceptions.ResourceExhausted):
                wait_time = self.config.retry_base_delay_seconds * (2 ** attempt)
                logger.warning(f"Rate limited, waiting {wait_time}s before retry {attempt + 1}/{self.config.max_retries}")
                time.sleep(wait_time)

            except (APITimeoutError, google_exceptions.DeadlineExceeded):
                wait_time = self.config.retry_base_delay_seconds * (2 ** attempt)
                logger.warning(f"Timeout, waiting {wait_time}s before retry {attempt + 1}/{self.config.max_retries}")
                time.sleep(wait_time)

            except (APIError, google_exceptions.GoogleAPIError) as e:
                logger.error(f"API error on attempt {attempt + 1}: {e}")
                if attempt < self.config.max_retries - 1:
                    time.sleep(self.config.retry_base_delay_seconds * (2 ** attempt))
                else:
                    return None

            except Exception as e:
                logger.error(f"Unexpected error during classification: {e}")
                return None

        if not raw_response:
            logger.error("Failed to get response from AI after all retries")
            return None

        # Parse the JSON response
        result = self._parse_response(raw_response)
        if result is None:
            logger.error("Failed to parse AI response as JSON")
            return None

        return result

    def _classify_openai(self, prompt: str) -> Optional[str]:
        """
        Call OpenAI API to classify email.

        No completion cap is sent. The caps here were 500 for a classification and 900
        for row disambiguation, sized for gpt-4o-mini's plain output. On the newer models
        that budget also covers reasoning tokens, so a model that deliberates can spend
        the whole allowance thinking and return empty text — a silent failure, where the
        runaway output the cap guarded against would at least be visible. Verdicts run
        about 67 tokens, so there is nothing to cap.
        """
        client = self._get_openai_client()

        logger.debug(f"Calling OpenAI {self.config.openai_model}")
        start_time = time.time()

        response = client.chat.completions.create(
            model=self.config.openai_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,  # Low temperature for consistent classification
        )

        elapsed = time.time() - start_time
        logger.debug(f"OpenAI response received in {elapsed:.2f}s")

        if response.choices and response.choices[0].message.content:
            return response.choices[0].message.content.strip()

        return None

    def _classify_gemini(self, prompt: str) -> Optional[str]:
        """Call Gemini API to classify email. Uncapped, matching the OpenAI path."""
        client = self._get_gemini_client()

        logger.debug(f"Calling Gemini {self.config.gemini_model}")
        start_time = time.time()

        response = client.models.generate_content(
            model=self.config.gemini_model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.1,
            )
        )

        elapsed = time.time() - start_time
        logger.debug(f"Gemini response received in {elapsed:.2f}s")

        if response.text:
            return response.text.strip()

        return None

    def _parse_response(self, response: str) -> Optional[EmailClassification]:
        """
        Parse the AI response as JSON and create EmailClassification.

        Args:
            response: Raw response text from AI

        Returns:
            EmailClassification if valid, None on failure
        """
        # Remove markdown code blocks if present
        text = response.strip()

        if text.startswith("```"):
            first_newline = text.find("\n")
            if first_newline != -1:
                text = text[first_newline + 1:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        # Try to parse as JSON
        data = None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON object from the response
            match = re.search(r'\{[\s\S]*\}', text)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    pass

        if data is None:
            logger.error(f"Failed to parse response as JSON. First 300 chars: {text[:300]}")
            return None

        # Validate required fields
        category = data.get("category", "unknown")
        if category not in ("confirmation", "oa", "phone", "technical", "stage_done",
                            "offer", "rejection", "unknown"):
            logger.warning(f"Unknown category '{category}', treating as 'unknown'")
            category = "unknown"

        # A stage_done with no recognisable stage says nothing actionable, so it is
        # demoted rather than left to be interpreted downstream.
        stage_completed = None
        if category == "stage_done":
            raw_stage = str(data.get("stage_completed") or "").strip().lower()
            stage_completed = {s.lower(): s for s in COMPLETABLE_STAGES}.get(raw_stage)
            if stage_completed is None:
                logger.warning(
                    f"stage_done without a valid stage_completed ({raw_stage!r}); "
                    f"treating as 'unknown'"
                )
                category = "unknown"

        confidence = data.get("confidence", 0.0)
        if not isinstance(confidence, (int, float)):
            try:
                confidence = float(confidence)
            except (ValueError, TypeError):
                confidence = 0.0

        # Get company candidates (array) or fall back to old company_name field
        company_candidates = data.get("company_candidates", [])
        if not company_candidates:
            # Backward compatibility: check for old company_name field
            old_name = data.get("company_name", "")
            if old_name:
                company_candidates = [old_name]
            else:
                logger.warning("No company names extracted from email")

        company_candidates = drop_hostname_candidates(company_candidates)

        return EmailClassification(
            category=category,
            confidence=confidence,
            company_candidates=company_candidates,
            stage_completed=stage_completed,
            position=data.get("position"),
            date_mentioned=data.get("date_mentioned"),
            time_mentioned=data.get("time_mentioned"),
            action_required=data.get("action_required"),
            key_details=data.get("key_details"),
            note=data.get("note"),
        )

    @property
    def row_match_prompt_template(self) -> str:
        """Load and cache the row-disambiguation prompt template."""
        if self._row_match_prompt is None:
            prompt_path = self.config.prompts_dir / "job_row_match.txt"
            if not prompt_path.exists():
                raise FileNotFoundError(f"Prompt template not found: {prompt_path}")
            self._row_match_prompt = prompt_path.read_text(encoding="utf-8")
        return self._row_match_prompt

    def choose_job_row(
        self, email: EmailMessage, candidates: List[Dict[str, Any]]
    ) -> Optional[int]:
        """
        Ask the AI which tracker row a status email belongs to.

        Called only when text lookup has already failed to single out a row, because
        the tracker and the email word the same role differently ("Software Engineer
        Intern" vs "Software Engineer Internship"). Deciding that is language
        interpretation, so the AI does it rather than a similarity heuristic.

        Args:
            email: The status email being matched.
            candidates: Rows to choose between, each with "row", "company", "position".

        Returns:
            The chosen row number, or None when the AI declines to choose or the
            call fails. None is a normal outcome: the caller then flags the email
            for review, which is what it would have done anyway.
        """
        if not candidates:
            return None

        body = email.body_text
        if not body and email.body_html:
            soup = BeautifulSoup(email.body_html, "html.parser")
            for element in soup(["script", "style"]):
                element.decompose()
            body = soup.get_text(separator="\n", strip=True)

        listing = "\n".join(
            f"- row {c['row']}: {c.get('company', '')} | {c.get('position', '')}"
            for c in candidates
        )
        prompt = (
            self.row_match_prompt_template
            .replace("{candidates}", listing)
            .replace("{subject}", email.subject or "")
            .replace("{sender}", f"{email.sender} <{email.sender_email}>")
            .replace("{date}", email.date.strftime("%Y-%m-%d %H:%M"))
            .replace("{body}", body or "")
        )

        valid_rows = {c["row"] for c in candidates}
        try:
            if self.config.ai_provider == "openai":
                raw = self._classify_openai(prompt)
            else:
                raw = self._classify_gemini(prompt)
        except Exception as e:
            # Never fatal: the caller falls back to flagging the email for review.
            logger.warning(f"Row disambiguation call failed: {e}")
            return None

        if not raw:
            return None

        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            logger.warning("Row disambiguation returned no JSON object")
            return None

        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning("Row disambiguation returned unparseable JSON")
            return None

        row = data.get("row")
        if row is None:
            logger.info(f"AI declined to choose a row: {data.get('reason', 'no reason given')}")
            return None

        try:
            row = int(row)
        except (TypeError, ValueError):
            logger.warning(f"Row disambiguation returned a non-numeric row: {row!r}")
            return None

        # Only trust a row that was actually offered, so a hallucinated number
        # cannot write a status onto an unrelated application.
        if row not in valid_rows:
            logger.warning(f"Row disambiguation returned row {row}, which was not a candidate")
            return None

        logger.info(f"AI matched row {row}: {data.get('reason', '')}")
        return row


# Singleton instance
_classifier: Optional[EmailClassifier] = None


def get_classifier(config: Optional[Config] = None) -> EmailClassifier:
    """Get the global EmailClassifier instance."""
    global _classifier
    if _classifier is None:
        _classifier = EmailClassifier(config)
    return _classifier
