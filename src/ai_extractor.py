"""
AI-powered job data extraction from scraped page content.
Supports OpenAI (GPT-4o-mini) and Google Gemini (1.5-flash).
"""

import json
import logging
import time
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Union

from openai import OpenAI, APIError, RateLimitError, APITimeoutError
from google import genai
from google.genai import types as genai_types
from google.api_core import exceptions as google_exceptions

from .config import Config, get_config


logger = logging.getLogger(__name__)


@dataclass
class DegreeRequirement:
    """Degree requirement extracted from job posting."""
    level: Optional[str] = None  # Masters | PhD | MBA | null
    type: Optional[str] = None   # required | preferred | null


@dataclass
class ExtractedJob:
    """Structured job data extracted from a job posting page."""
    # Required fields
    company: str
    title: str

    # Job type and model
    job_type: Optional[str] = None  # Internship | Full-Time | Part-Time | Contract
    work_model: Optional[str] = None  # Remote | Hybrid | On-site
    is_remote: Optional[bool] = None
    locations: List[str] = field(default_factory=list)

    # Salary
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_period: Optional[str] = None  # hourly | monthly | yearly
    currency: Optional[str] = None

    # Eligibility requirements
    class_standing_requirement: Optional[str] = None  # Freshman | Sophomore | Junior | Senior | Graduate
    graduation_timeline: Optional[str] = None
    season_year: Optional[str] = None  # Summer 2025, Fall 2025, etc.
    work_authorization: Optional[str] = None
    sponsorship_available: Optional[bool] = None
    gpa_requirement: Optional[float] = None
    degree_requirement: Optional[DegreeRequirement] = None

    # Job details
    company_job_id: Optional[str] = None
    job_category: Optional[str] = None  # Software Engineering | Product Management | Data Science/AI/ML | Quantitative Finance | Hardware Engineering
    apply_url: Optional[str] = None
    posted_date: Optional[str] = None
    deadline: Optional[str] = None

    # Skills and majors
    required_skills: List[str] = field(default_factory=list)
    preferred_skills: List[str] = field(default_factory=list)
    required_majors: List[str] = field(default_factory=list)

    # Summary
    description_summary: Optional[str] = None

    # Metadata
    source_url: Optional[str] = None
    raw_response: Optional[str] = None  # For debugging


class AIExtractor:
    """
    Extracts structured job data from scraped page content using AI.

    Supports OpenAI and Gemini, with retry logic and JSON validation.
    """

    def __init__(self, config: Optional[Config] = None):
        """
        Initialize the AI extractor.

        Args:
            config: Optional config object. Uses global config if not provided.
        """
        self.config = config or get_config()
        self._prompt_template: Optional[str] = None
        self._openai_client: Optional[OpenAI] = None
        self._gemini_client = None

    @property
    def prompt_template(self) -> str:
        """Load and cache the extraction prompt template."""
        if self._prompt_template is None:
            prompt_path = self.config.prompts_dir / "job_extraction.txt"
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

    def extract(self, content: str, source_url: str = "") -> List[ExtractedJob]:
        """
        Extract structured job data from page content.

        Args:
            content: Scraped page text content
            source_url: URL of the job posting (for metadata)

        Returns:
            List of ExtractedJob objects. May contain multiple jobs if the posting
            has multiple positions. Returns empty list if extraction failed.
        """
        if not content or not content.strip():
            logger.warning("Empty content provided for extraction")
            return []

        # Build the prompt
        prompt = self.prompt_template.replace("{content}", content)
        prompt = prompt.replace("{today_date}", date.today().isoformat())

        # Call the appropriate AI provider
        if self.config.ai_provider == "openai":
            model_name = self.config.openai_model
        else:
            model_name = self.config.gemini_model

        raw_response: Optional[str] = None

        for attempt in range(self.config.max_retries):
            try:
                if self.config.ai_provider == "openai":
                    raw_response = self._extract_openai(prompt)
                else:
                    raw_response = self._extract_gemini(prompt)

                if raw_response:
                    break

            except (RateLimitError, google_exceptions.ResourceExhausted) as e:
                wait_time = self.config.retry_base_delay_seconds * (2 ** attempt)
                logger.warning(f"Rate limited, waiting {wait_time}s before retry {attempt + 1}/{self.config.max_retries}")
                time.sleep(wait_time)

            except (APITimeoutError, google_exceptions.DeadlineExceeded) as e:
                wait_time = self.config.retry_base_delay_seconds * (2 ** attempt)
                logger.warning(f"Timeout, waiting {wait_time}s before retry {attempt + 1}/{self.config.max_retries}")
                time.sleep(wait_time)

            except (APIError, google_exceptions.GoogleAPIError) as e:
                logger.error(f"API error on attempt {attempt + 1}: {e}")
                if attempt < self.config.max_retries - 1:
                    time.sleep(self.config.retry_base_delay_seconds * (2 ** attempt))
                else:
                    return []

            except Exception as e:
                logger.error(f"Unexpected error during extraction: {e}")
                return []

        if not raw_response:
            logger.error("Failed to get response from AI after all retries")
            return []

        # Log full response for debugging
        logger.debug(f"Raw AI response ({len(raw_response)} chars):\n{raw_response}")

        # Parse the JSON response (can be single object or array)
        parsed = self._parse_response(raw_response)
        if parsed is None:
            logger.error("Failed to parse AI response as JSON")
            return []

        # Validate and create ExtractedJob(s)
        jobs = self._validate_jobs(parsed, source_url, raw_response)

        for job in jobs:
            logger.info(f"Successfully extracted: {job.company} - {job.title}")

        if not jobs:
            logger.warning("No valid jobs extracted from response")

        return jobs

    def _extract_openai(self, prompt: str) -> Optional[str]:
        """
        Call OpenAI API to extract job data.

        Args:
            prompt: The complete prompt with job content

        Returns:
            Raw response text or None on failure
        """
        client = self._get_openai_client()

        logger.debug(f"Calling OpenAI {self.config.openai_model}")
        start_time = time.time()

        # Build params - only include max_tokens if configured
        params = {
            "model": self.config.openai_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,  # Low temperature for consistent extraction
        }
        if self.config.openai_max_tokens:
            params["max_tokens"] = self.config.openai_max_tokens

        response = client.chat.completions.create(**params)

        elapsed = time.time() - start_time
        logger.debug(f"OpenAI response received in {elapsed:.2f}s")

        if response.choices and response.choices[0].message.content:
            return response.choices[0].message.content.strip()

        return None

    def _extract_gemini(self, prompt: str) -> Optional[str]:
        """
        Call Gemini API to extract job data.

        Args:
            prompt: The complete prompt with job content

        Returns:
            Raw response text or None on failure
        """
        client = self._get_gemini_client()

        logger.debug(f"Calling Gemini {self.config.gemini_model}")
        start_time = time.time()

        # Build config - only include max_output_tokens if configured
        gen_config_params = {"temperature": 0.1}
        if self.config.gemini_max_output_tokens:
            gen_config_params["max_output_tokens"] = self.config.gemini_max_output_tokens

        response = client.models.generate_content(
            model=self.config.gemini_model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(**gen_config_params)
        )

        elapsed = time.time() - start_time
        logger.debug(f"Gemini response received in {elapsed:.2f}s")

        if response.text:
            return response.text.strip()

        return None

    def _parse_response(self, response: str) -> Optional[Union[Dict[str, Any], List[Dict[str, Any]]]]:
        """
        Parse the AI response as JSON.

        Handles common issues like markdown code blocks and trailing text.
        Can return either a single dict or a list of dicts (for multi-position postings).

        Args:
            response: Raw response text from AI

        Returns:
            Parsed dictionary, list of dictionaries, or None on failure
        """
        # Remove markdown code blocks if present
        text = response.strip()

        # Remove ```json ... ``` wrapper
        if text.startswith("```"):
            # Find the end of the first line (```json or ```)
            first_newline = text.find("\n")
            if first_newline != -1:
                text = text[first_newline + 1:]
            # Remove closing ```
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        # Try to parse as JSON (can be object or array)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON parse error: {e}")

            # Try to extract JSON array from the response
            # Look for [ ... ] pattern first (array)
            match = re.search(r'\[[\s\S]*\]', text)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass

            # Try to extract JSON object from the response
            # Look for { ... } pattern
            match = re.search(r'\{[\s\S]*\}', text)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass

            logger.error(f"Failed to parse response as JSON. First 500 chars: {text[:500]}")
            return None

    def _validate_jobs(
        self,
        data: Union[Dict[str, Any], List[Dict[str, Any]]],
        source_url: str,
        raw_response: str
    ) -> List[ExtractedJob]:
        """
        Validate extracted data and create ExtractedJob(s).

        Handles both single job objects and arrays of jobs (for multi-position postings).
        Also handles nested structure where jobs are in a "jobs" array with shared fields at top level.

        Args:
            data: Parsed JSON data (single dict or list of dicts)
            source_url: Original URL of the job posting
            raw_response: Raw AI response (for debugging)

        Returns:
            List of valid ExtractedJob objects
        """
        jobs = []

        # Handle nested structure: {"company": "...", "jobs": [...]}
        if isinstance(data, dict) and "jobs" in data and isinstance(data["jobs"], list):
            logger.info(f"Nested multi-position posting detected: {len(data['jobs'])} positions")
            # Extract shared fields from top level
            shared_fields = {k: v for k, v in data.items() if k != "jobs"}
            for i, job_data in enumerate(data["jobs"]):
                if isinstance(job_data, dict):
                    # Merge shared fields into each job (job-specific fields take precedence)
                    merged = {**shared_fields, **job_data}
                    job = self._validate_single_job(merged, source_url, raw_response)
                    if job:
                        jobs.append(job)
                else:
                    logger.warning(f"Invalid job data at index {i}: expected dict, got {type(job_data)}")
        # Handle array of jobs (multi-position posting)
        elif isinstance(data, list):
            logger.info(f"Multi-position posting detected: {len(data)} positions")
            for i, job_data in enumerate(data):
                if isinstance(job_data, dict):
                    job = self._validate_single_job(job_data, source_url, raw_response)
                    if job:
                        jobs.append(job)
                else:
                    logger.warning(f"Invalid job data at index {i}: expected dict, got {type(job_data)}")
        # Handle single job object
        elif isinstance(data, dict):
            job = self._validate_single_job(data, source_url, raw_response)
            if job:
                jobs.append(job)
        else:
            logger.error(f"Unexpected data type: {type(data)}")

        return jobs

    def _validate_single_job(
        self,
        data: Dict[str, Any],
        source_url: str,
        raw_response: str
    ) -> Optional[ExtractedJob]:
        """
        Validate a single job's extracted data and create ExtractedJob.

        Args:
            data: Parsed JSON data for a single job
            source_url: Original URL of the job posting
            raw_response: Raw AI response (for debugging)

        Returns:
            ExtractedJob if valid, None if required fields missing
        """
        # Check required fields
        company = data.get("company")
        title = data.get("title")

        if not company or not isinstance(company, str) or not company.strip():
            logger.warning("Missing or invalid 'company' field in extraction")
            return None

        if not title or not isinstance(title, str) or not title.strip():
            logger.warning("Missing or invalid 'title' field in extraction")
            return None

        # Parse degree requirement
        degree_req = None
        if data.get("degree_requirement"):
            deg_data = data["degree_requirement"]
            if isinstance(deg_data, dict):
                degree_req = DegreeRequirement(
                    level=deg_data.get("level"),
                    type=deg_data.get("type")
                )

        # Determine apply_url: use AI-extracted URL, fallback to source_url
        apply_url = data.get("apply_url")
        if not apply_url and source_url:
            apply_url = source_url
            logger.debug(f"Using source_url as apply_url: {source_url}")

        # Create ExtractedJob
        try:
            job = ExtractedJob(
                company=company.strip(),
                title=title.strip(),
                job_type=data.get("job_type"),
                work_model=data.get("work_model"),
                is_remote=data.get("is_remote"),
                locations=data.get("locations") or [],
                salary_min=self._parse_number(data.get("salary_min")),
                salary_max=self._parse_number(data.get("salary_max")),
                salary_period=data.get("salary_period"),
                currency=data.get("currency"),
                class_standing_requirement=data.get("class_standing_requirement"),
                graduation_timeline=data.get("graduation_timeline"),
                season_year=data.get("season_year"),
                work_authorization=data.get("work_authorization"),
                sponsorship_available=data.get("sponsorship_available"),
                gpa_requirement=self._parse_number(data.get("gpa_requirement")),
                degree_requirement=degree_req,
                company_job_id=data.get("company_job_id"),
                job_category=data.get("job_category"),
                apply_url=apply_url,
                posted_date=data.get("posted_date"),
                deadline=data.get("deadline"),
                required_skills=data.get("required_skills") or [],
                preferred_skills=data.get("preferred_skills") or [],
                required_majors=data.get("required_majors") or [],
                description_summary=data.get("description_summary"),
                source_url=source_url,
                raw_response=raw_response,
            )
            return job

        except Exception as e:
            logger.error(f"Error creating ExtractedJob: {e}")
            return None

    def _parse_number(self, value: Any) -> Optional[float]:
        """Parse a value as a number, returning None if invalid."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                # Remove common formatting
                cleaned = value.replace(",", "").replace("$", "").strip()
                return float(cleaned)
            except ValueError:
                return None
        return None


# Singleton instance
_extractor: Optional[AIExtractor] = None


def get_extractor(config: Optional[Config] = None) -> AIExtractor:
    """
    Get the global AIExtractor instance.

    Args:
        config: Optional config to use. If provided on first call, will be used
                for the singleton. Subsequent calls ignore this parameter.

    Returns:
        AIExtractor singleton instance
    """
    global _extractor
    if _extractor is None:
        _extractor = AIExtractor(config)
    return _extractor


def reset_extractor() -> None:
    """Reset the singleton extractor (useful for testing)."""
    global _extractor
    _extractor = None
