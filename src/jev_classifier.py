"""
Jev (TypeSafe AI) as a shadow email classifier.

Jev answers a question by choosing among labels fixed in advance and returning a
probability for each, instead of writing its answer as text. That makes its confidence
a reading of how the probability is spread rather than a number the model chose to
print — the LLM classifier's `confidence` is generated text like the rest of its JSON,
and it sent the Meta application receipt to "unknown" without a flicker of doubt.

This module only produces Jev's answer. It decides nothing: check_gmail.py records the
answer beside the LLM's (see jev_log.py) and carries on exactly as before.

Jev cannot write text, so the company, position and dates still come from the LLM.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient

from .config import Config, get_config
from .gmail import EmailMessage, body_as_text

logger = logging.getLogger(__name__)

# The SDK's debug log carries request bodies unredacted, which here means whole emails.
# Held at INFO so a root logger turned up to DEBUG cannot copy mail into logs/.
logging.getLogger("typesafe_sdk").setLevel(logging.INFO)

# A shadow call must never hold up the run. Jev quotes 70–500 ms; one retry absorbs a
# blip, and anything slower is recorded as no answer rather than waited on.
TIMEOUT_SECONDS = 3.0
RETRY = RetryPolicy(max_retries=1)

QUESTION = "category"
INSTRUCTIONS = (
    "This email arrived in a job applicant's inbox. Which of these best describes what it "
    "says about one of the applicant's own job applications? Judge by what the body says "
    "happened, not by the subject line alone."
)

# Same eight categories as prompts/email_classification.txt, in the same words where
# possible, so a disagreement reflects the model rather than a different question.
CRITERIA: Dict[str, str] = {
    "confirmation": "Application received or submitted — a first acknowledgement that the "
                    "application arrived, even if it adds that it is under review",
    "oa": "Invitation to an online assessment (HackerRank, CodeSignal, etc.)",
    "phone": "Invitation to a phone screen or phone interview",
    "technical": "Invitation to a technical interview (onsite, virtual, coding round)",
    "stage_done": "A receipt for a stage the applicant has already FINISHED — an assessment "
                  "submitted or an interview completed — not an invitation to do one",
    "offer": "A job offer",
    "rejection": "The application was declined",
    "unknown": "None of the above: not about one of the applicant's applications, or about "
               "one but reporting no change of stage (portal or profile setup, info session, "
               "recruiter check-in, newsletter, job alert, coursework)",
}
CATEGORIES = tuple(CRITERIA)


@dataclass
class JevResult:
    """Jev's answer for one email."""
    label: str
    confidence: float
    probabilities: Dict[str, float] = field(default_factory=dict)
    model: str = ""
    input_tokens: Optional[int] = None

    def status_probability(self) -> float:
        """Total probability on every label that would change a row — all but unknown."""
        return sum(p for label, p in self.probabilities.items() if label != "unknown")


class JevClassifier:
    """Asks Jev which category an email belongs to. Never raises."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()
        self._client: Optional[TypeSafeClient] = None

    def _get_client(self) -> TypeSafeClient:
        if self._client is None:
            self._client = TypeSafeClient(
                api_key=self.config.typesafe_api_key,
                retry=RETRY,
                timeout=TIMEOUT_SECONDS,
            )
        return self._client

    @staticmethod
    def build_state(email: EmailMessage) -> Dict[str, str]:
        """
        The email as Jev's state: the same subject, sender, date and full body the LLM
        classifier is given, so the two are compared on identical input.
        """
        return {
            "subject": email.subject or "",
            "sender": f"{email.sender} <{email.sender_email}>",
            "date": email.date.strftime("%Y-%m-%d %H:%M"),
            "body": body_as_text(email),
        }

    def classify(self, email: EmailMessage) -> Optional[JevResult]:
        """
        Jev's category for this email, or None when no answer could be had.

        None covers everything — no key, no body, a timeout, an API error, a response
        that does not parse. Shadow mode is observation; a Jev problem must not cost the
        run the emails the LLM can still handle.
        """
        if not self.config.typesafe_api_key:
            return None
        state = self.build_state(email)
        if not state["body"].strip():
            return None
        try:
            result = self._get_client().system_one(
                state,
                {QUESTION: Choice(instructions=INSTRUCTIONS, criteria=CRITERIA)},
            )
            answer = result.choices[QUESTION]
            return JevResult(
                label=answer.choice,
                confidence=float(answer.confidence),
                probabilities={k: float(v) for k, v in answer.probabilities.items()},
                model=result.model,
                input_tokens=result.usage.input_tokens,
            )
        except Exception as e:
            logger.warning(f"Jev classification failed for '{email.subject}': {type(e).__name__}: {e}")
            return None


def get_jev_classifier(config: Optional[Config] = None) -> JevClassifier:
    """A JevClassifier for the given or global config."""
    return JevClassifier(config)
