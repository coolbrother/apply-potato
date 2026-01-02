"""
AI-powered email classification for ApplyPotato.
Classifies job-related emails into categories (confirmation, OA, interview, offer, rejection).
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from openai import OpenAI, APIError, RateLimitError, APITimeoutError
from google import genai
from google.genai import types as genai_types
from google.api_core import exceptions as google_exceptions

from .config import Config, get_config
from .gmail import EmailMessage


logger = logging.getLogger(__name__)


@dataclass
class EmailClassification:
    """Result of email classification."""
    category: str  # confirmation, oa, phone, technical, offer, rejection, unknown
    confidence: float
    company_candidates: list  # List of company names found in email
    position: Optional[str] = None
    date_mentioned: Optional[str] = None
    time_mentioned: Optional[str] = None
    action_required: Optional[str] = None
    key_details: Optional[str] = None


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
            from bs4 import BeautifulSoup
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
        """Call OpenAI API to classify email."""
        client = self._get_openai_client()

        logger.debug(f"Calling OpenAI {self.config.openai_model}")
        start_time = time.time()

        response = client.chat.completions.create(
            model=self.config.openai_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,  # Low temperature for consistent classification
            max_tokens=500,
        )

        elapsed = time.time() - start_time
        logger.debug(f"OpenAI response received in {elapsed:.2f}s")

        if response.choices and response.choices[0].message.content:
            return response.choices[0].message.content.strip()

        return None

    def _classify_gemini(self, prompt: str) -> Optional[str]:
        """Call Gemini API to classify email."""
        client = self._get_gemini_client()

        logger.debug(f"Calling Gemini {self.config.gemini_model}")
        start_time = time.time()

        response = client.models.generate_content(
            model=self.config.gemini_model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=500,
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
        if category not in ("confirmation", "oa", "phone", "technical", "offer", "rejection", "unknown"):
            logger.warning(f"Unknown category '{category}', treating as 'unknown'")
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

        return EmailClassification(
            category=category,
            confidence=confidence,
            company_candidates=company_candidates,
            position=data.get("position"),
            date_mentioned=data.get("date_mentioned"),
            time_mentioned=data.get("time_mentioned"),
            action_required=data.get("action_required"),
            key_details=data.get("key_details"),
        )


# Singleton instance
_classifier: Optional[EmailClassifier] = None


def get_classifier(config: Optional[Config] = None) -> EmailClassifier:
    """Get the global EmailClassifier instance."""
    global _classifier
    if _classifier is None:
        _classifier = EmailClassifier(config)
    return _classifier
