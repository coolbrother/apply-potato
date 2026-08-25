#!/usr/bin/env python3
"""
Gmail status tracking for ApplyPotato.

Monitors Gmail for job application status updates and updates Google Sheets.

Reads from every account listed in GMAIL_ACCOUNTS (or the single authorized
account when that setting is blank).

Workflow:
1. Fetch recent emails from Gmail (with privacy filters)
2. Classify emails with AI (confirmation, OA, interview, offer, rejection)
3. Match emails to existing jobs by company name
4. Update job status in Google Sheets

Usage:
    python check_gmail.py              # Run once
    python check_gmail.py --scheduled  # Run on schedule (every N minutes)
    python check_gmail.py --auth       # One-time OAuth for each configured account
"""

import argparse
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from apscheduler.schedulers.blocking import BlockingScheduler

from src.config import get_config, Config
from src.logging_config import setup_logging
from src.gmail import GmailClient, get_gmail_clients, EmailMessage, message_text
from src.email_filters import apply_privacy_filters
from src.email_classifier import EmailClassifier, get_classifier, EmailClassification
from src.needs_review import (
    REASON_ALL_RETIRED,
    REASON_AMBIGUOUS,
    REASON_DUPLICATE_ROWS,
    REASON_NO_COMPANY,
    REASON_UNTRACKED,
    record_needs_review,
)
from src.sheets import (
    SheetsClient,
    get_sheets_client,
    JobRow,
    add_stage,
    event_label,
    normalize_date,
    parse_sheet_datetime,
    reached_stage,
    requisition_ids,
    shows_application,
    STATUS_APPLIED,
    STATUS_GHOSTED,
    STATUS_NEW,
    STATUS_OA,
    STATUS_OFFER,
    STATUS_PHONE,
    STATUS_REJECTED,
    STATUS_TECHNICAL,
)
from src.notifications import is_dream_company, notify_status_change
from src.email_archive import save_status_email
from src.job_desc import commit_and_push_job_folder


logger = logging.getLogger(__name__)


# Mapping of email categories to job status values
CATEGORY_TO_STATUS = {
    "confirmation": "Applied",
    "oa": "OA",
    "phone": "Phone",
    "technical": "Technical",
    "offer": "Offer",
    "rejection": "Rejected",
}

# How far along the pipeline each status is. A row may move up this ladder but never
# back down: an application does not un-progress. Timestamps cannot enforce this on
# their own, because the mail that would walk a row backwards is often genuinely newer
# — Castleton sent the assessment invite and the "Thank You for Applying" confirmation
# three seconds apart, the confirmation second, and it reset row 404 from OA to Applied.
STATUS_RANK = {
    STATUS_NEW: 0,
    STATUS_APPLIED: 1,
    STATUS_OA: 2,
    STATUS_PHONE: 3,
    STATUS_TECHNICAL: 4,
    STATUS_OFFER: 5,
}

# Outcomes that end the process. They can arrive at any stage — a rejection after a
# final round is still a rejection — so they are never treated as a regression.
TERMINAL_STATUSES = {STATUS_REJECTED, STATUS_GHOSTED}

# Mapping of email categories to date columns. "confirmation" is handled separately
# in _update_job_status: application_date is single-valued and comes from the email's
# arrival time, while the rest are scheduled events that may accumulate.
CATEGORY_TO_DATE_COLUMN = {
    "confirmation": "application_date",
    "oa": "oa_date",
    "phone": "phone_date",
    "technical": "tech_date",
}

@dataclass
class MatchOutcome:
    """
    The result of looking for the Sheet row an email belongs to.

    A bare Optional[JobRow] loses the one thing worth reporting when the lookup
    fails — *why* it failed, and which rows tied. Those go into the review log.
    """

    job: Optional[JobRow] = None
    reason: str = ""                              # blank when job is not None
    company: str = ""                             # candidate the reason is about
    candidates: List[JobRow] = field(default_factory=list)

    # Set only on a successful match that the AI picked out of exact duplicate rows.
    # The status still gets written; these are logged so the digest can tell the user
    # the twins exist and mark_canonical.py should collapse them.
    duplicates: List[JobRow] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return self.job is not None


class GmailChecker:
    """
    Main Gmail checking pipeline.

    Orchestrates the flow from Gmail → Classification → Sheets update.
    """

    def __init__(self, config: Optional[Config] = None, reprocess: bool = False):
        """
        Initialize the Gmail checker.

        Args:
            config: Optional config object. Uses global config if not provided.
            reprocess: If True, reprocess all emails ignoring processed_emails.json
        """
        self.config = config or get_config()
        self.reprocess = reprocess
        self.gmail_clients = get_gmail_clients()
        self.classifier = get_classifier(self.config)
        self.sheets_client = get_sheets_client()

        # Rows the user struck through to retire them. Refreshed once per run()
        # rather than per email — it costs an API call and cannot change mid-run.
        self._struck_rows: set = set()

        # Stats
        self.stats = {
            "accounts_checked": 0,
            "emails_fetched": 0,
            "filtered_out": 0,
            "classified": 0,
            "matched": 0,
            "updated": 0,
            "no_match": 0,
            "ambiguous": 0,
            "duplicate_rows": 0,
            "unknown_category": 0,
            "stale_skipped": 0,
            "status_regression_blocked": 0,
            "stages_completed": 0,
        }

    def _find_matching_job(
        self,
        classification: EmailClassification,
        email: Optional[EmailMessage] = None,
    ) -> MatchOutcome:
        """
        Find a matching job in Sheets by trying all company candidates.

        Never guesses between tied rows — a wrong status write is worse than no
        write. The first tie encountered is remembered so the caller can report it,
        then the remaining candidates are still tried, since a later candidate may
        resolve to exactly one row.

        Text lookup is containment matching, which cannot see that "Software Engineer
        Internship" and "Software Engineer Intern" are one role. When every candidate
        has been tried and only a tie remains, the AI is asked to pick from the tied
        rows; it answers null unless one is clearly right.

        A requisition id is tried first, because it names the posting where a company
        name names the employer, and an ATS routinely signs for the parent rather than
        the subsidiary that owns the job.

        Args:
            classification: Email classification with company candidates and position
            email: The email itself, needed to ask the AI to break a tie. Without it
                the tie is reported unresolved, exactly as before.

        Returns:
            A MatchOutcome: the row when exactly one candidate resolved, otherwise
            the reason no row could be chosen.
        """
        position = classification.position
        tie: Optional[MatchOutcome] = None
        retired_only = False  # A company whose rows all exist but are all struck
        named_struck = False  # The row naming this email's position was struck

        def live(rows: List[JobRow]) -> List[JobRow]:
            """Drop rows the user struck through, remembering if that emptied the list."""
            nonlocal retired_only
            kept = [row for row in rows if row.row_number not in self._struck_rows]
            if rows and not kept:
                retired_only = True
            return kept

        # A requisition id quoted in the email and stored in a row's URL is the one
        # identifier both sides agree on. Only an unambiguous hit is taken; anything
        # else falls through to the company candidates untouched.
        #
        # Uniqueness is judged over every row, struck ones included, and only then is
        # the survivor checked for strikethrough. Doing it the other way round lets a
        # struck row collapse a genuine ambiguity into false certainty: a Handshake job
        # alert listing several IMC postings quotes three ids spanning six rows, five of
        # them struck, and would otherwise have been read as a receipt for the sixth.
        if email is not None:
            by_id = self.sheets_client.find_jobs_by_requisition_id(
                requisition_ids(message_text(email))
            )
            if len(by_id) == 1:
                unstruck = live(by_id)
                if unstruck:
                    logger.info(
                        f"Matched by requisition id: row {unstruck[0].row_number} "
                        f"({unstruck[0].company})"
                    )
                    return MatchOutcome(job=unstruck[0])
            elif len(by_id) > 1:
                logger.debug(
                    f"Requisition id matched {len(by_id)} rows, falling back to company"
                )

        if not classification.company_candidates:
            return MatchOutcome(reason=REASON_NO_COMPANY)

        # Try each company candidate
        for company in classification.company_candidates:
            # Try company + position first (if position available)
            if position:
                named = self.sheets_client.find_jobs_by_company_and_position(company, position)
                matches = live(named)
                if len(matches) == 1:
                    logger.debug(f"Matched by company + position: {company} + {position}")
                    return MatchOutcome(job=matches[0])
                elif len(matches) > 1:
                    logger.warning(f"Multiple jobs match '{company}' + '{position}', trying next candidate")
                    tie = tie or MatchOutcome(
                        reason=REASON_AMBIGUOUS, company=company, candidates=matches
                    )
                    continue
                elif named:
                    # The email names a position, rows carry that exact position, and
                    # every one of them is struck. The company lookup below still runs:
                    # striking the position match is exactly how a canonical row is
                    # nominated, and that row often carries a different title, so
                    # abandoning the search here would break the mark-canonical
                    # workflow (see test_strikethrough).
                    #
                    # What is given up is the *guess*. If the widened lookup lands on
                    # one live row, that is the canonical row and it is taken. If it
                    # ties, the AI is not asked to choose, because every remaining
                    # candidate is a role the email did not name — that is how a
                    # "Software Engineer Intern" receipt came to be written onto a
                    # "Trading Intern" row.
                    logger.info(
                        f"'{company}' + '{position}' matches only struck row(s) "
                        f"{[job.row_number for job in named]}; widening, but a tie "
                        f"will not be guessed at"
                    )
                    named_struck = True

            # Fall back to company only
            matches = live(self.sheets_client.find_jobs_by_company(company))

            # Try alternative company name formats if no match
            if not matches:
                alternatives = [
                    company.replace(" LLC", "").replace(" Inc", "").replace(" Corp", "").strip(),
                    company.split()[0] if " " in company else company,  # First word
                ]
                for alt in alternatives:
                    if alt != company:
                        matches = live(self.sheets_client.find_jobs_by_company(alt))
                        if matches:
                            logger.debug(f"Found match using alternative company name: {alt}")
                            break

            if len(matches) == 1:
                logger.debug(f"Matched by company only: {company}")
                return MatchOutcome(job=matches[0])
            elif len(matches) > 1:
                logger.warning(f"Multiple jobs for '{company}', trying next candidate")
                tie = tie or MatchOutcome(
                    reason=REASON_AMBIGUOUS, company=company, candidates=matches
                )
                continue

        # No match found with any candidate
        logger.info(f"No matching job found for companies: {classification.company_candidates}")
        if tie:
            # Text lookup could not single out a row, which usually means the tracker and
            # the email word the same role differently ("Software Engineer Intern" vs
            # "Software Engineer Internship"). Deciding whether two titles name the same
            # role is language interpretation, so ask the AI rather than score the strings.
            # It is told to answer null unless one row is clearly right, and a row it did
            # not offer is rejected, so the worst case is the review flag we already had.
            if email is not None and tie.candidates and not named_struck:
                chosen = self.classifier.choose_job_row(
                    email,
                    [
                        {"row": job.row_number, "company": job.company, "position": job.position}
                        for job in tie.candidates
                    ],
                )
                if chosen is not None:
                    for job in tie.candidates:
                        if job.row_number == chosen:
                            # If the rows it chose between were the same posting scraped
                            # twice, the pick was arbitrary between twins. Report them so
                            # the digest can say so; exact equality, not a similarity call.
                            key = (job.company.strip().lower(), job.position.strip().lower())
                            twins = [
                                other for other in tie.candidates
                                if (other.company.strip().lower(),
                                    other.position.strip().lower()) == key
                            ]
                            return MatchOutcome(
                                job=job, duplicates=twins if len(twins) > 1 else []
                            )

            # The AI declined, or named a row it was not offered. Before giving up: if
            # exactly one of the tied rows shows an application and the rest show
            # nothing at all, that is evidence, and better than abandoning the email.
            # An assessment platform's mail routinely names the employer and no role —
            # "confirms your submission to <company>" is the whole body — so position
            # matching has nothing to work with and every row of that employer ties.
            #
            # Withheld when the row naming the email's position was struck, because then
            # every remaining candidate is a role the email never mentioned, and picking
            # the applied one among those is the same wrong guess as picking any other.
            if not named_struck:
                applied = [job for job in tie.candidates if shows_application(job)]
                if len(applied) == 1:
                    logger.info(
                        f"Tie of {len(tie.candidates)} resolved on application evidence: "
                        f"row {applied[0].row_number} ({applied[0].company} — "
                        f"{applied[0].status or 'has an Application Date'})"
                    )
                    return MatchOutcome(job=applied[0])

            return tie
        return MatchOutcome(
            reason=REASON_ALL_RETIRED if retired_only else REASON_UNTRACKED,
            company=classification.company_candidates[0],
        )

    def _log_needs_review(
        self,
        client: GmailClient,
        email: EmailMessage,
        classification: EmailClassification,
        outcome: MatchOutcome,
    ) -> None:
        """
        Record an unmatchable status email in data/needs_review.json.

        The email is still marked processed by the caller, so this log is the only
        trace it ever arrived. Never fatal: a broken review log must not cost the
        run the emails it can still match.
        """
        try:
            written = record_needs_review(
                self.config.data_dir,
                message_id=email.message_id,
                reason=outcome.reason,
                account=client.label,
                sender=email.sender_email,
                subject=email.subject,
                category=classification.category,
                company=outcome.company,
                candidates=[
                    {"row": job.row_number, "position": job.position, "status": job.status}
                    for job in outcome.candidates
                ],
            )
        except OSError as e:
            logger.error(f"Could not write needs_review log: {e}")
            return

        if written:
            detail = f" ({len(outcome.candidates)} candidate rows)" if outcome.candidates else ""
            logger.info(f"Flagged for review [{outcome.reason}]: {email.subject[:60]}{detail}")

    @staticmethod
    def _local_naive(dt: Optional[datetime]) -> datetime:
        """
        Coerce an email timestamp to a local, timezone-naive datetime.

        parsedate_to_datetime() returns a tz-aware datetime carrying the *sender's*
        UTC offset. The Sheet stores local wall-clock times and daily_summary.py
        windows against local naive bounds, so a foreign offset would silently shift
        the recorded hour.
        """
        if dt is None:
            return datetime.now()
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt

    def _application_date_value(self, job: JobRow, email: EmailMessage) -> Optional[str]:
        """
        Build the timestamp for application_date, or None to leave the cell alone.

        Single-valued and write-once: the first confirmation is the real application
        date, so a later confirmation neither overwrites nor appends. Carries a time
        because column F is DATE_TIME and daily_summary.py windows Applied counts
        within the day.
        """
        if str(job.application_date or "").strip():
            logger.debug(
                f"application_date already set on row {job.row_number}, leaving as-is"
            )
            return None
        return self._local_naive(email.date).strftime("%m/%d/%Y %H:%M:%S")

    def _archive_status_email(self, job: JobRow, category: str, email: EmailMessage) -> None:
        """
        Copy an OA-or-later email into the job's folder, then push it.

        Wrapped whole: this is bookkeeping hanging off a status update that has already
        been written, so a missing directory, a git failure or an unwritable disk must
        not surface as a failed update.
        """
        try:
            path = save_status_email(
                job.row_number,
                job.company,
                category,
                email,
                self.config.job_desc_output_dir,
            )
            if path is None:
                return

            commit_and_push_job_folder(
                folder=path.parent,
                repo_dir=self.config.job_desc_output_dir,
                stem=path.parent.name,
            )
        except Exception as e:
            logger.warning(f"  Status email archiving failed for row {job.row_number}: {e}")

    def _is_status_regression(self, current: Optional[str], new_status: str) -> bool:
        """
        True when writing new_status would move the row backwards down the pipeline.

        The timestamp guard cannot catch this on its own. Two systems often mail within
        the same minute — an assessment invite from the testing vendor, the "Thank You
        for Applying" from the ATS — and the confirmation is frequently the newer of the
        two, so it passes the staleness check and resets a row that has already reached
        OA. That is what happened to row 404.

        Terminal outcomes are exempt as the *incoming* status: a rejection can arrive at
        any stage and is still the truth. As the *current* status they are the opposite —
        a floor nothing non-terminal may cross. Rejected and Ghosted are absent from
        STATUS_RANK, so before this they fell through the unrecognised-status guard below
        and any stage could overwrite them; that is how a misdirected assessment invite
        moved row 317 from Rejected back to OA. A row that has ended stays ended until a
        person says otherwise.

        An unrecognised status on either side returns False, so an unknown value is never
        silently swallowed.
        """
        if new_status in TERMINAL_STATUSES:
            return False

        if (current or "").strip() in TERMINAL_STATUSES:
            return True

        current_rank = STATUS_RANK.get((current or "").strip())
        new_rank = STATUS_RANK.get(new_status)
        if current_rank is None or new_rank is None:
            return False

        return new_rank <= current_rank

    def _is_newer_than_last_email(self, job: JobRow, email: EmailMessage) -> bool:
        """
        True when this email is allowed to modify the row.

        Gmail hands back messages newest-first and several can match one row in a
        single run, so without this an older email applied after a newer one silently
        walks the row backwards — a stale confirmation overwriting a fresh rejection.

        A blank cell (every row predating column V) means nothing has been recorded
        yet, so the email wins. Unparseable is treated the same way: one bad cell must
        not freeze a row forever. --reprocess deliberately bypasses the check, since
        re-walking old mail is exactly what that flag is for.
        """
        if self.reprocess:
            return True
        last = parse_sheet_datetime(job.last_email_time)
        if last is None:
            return True
        return self._local_naive(email.date) > last

    def _event_date_value(
        self,
        classification: EmailClassification,
        email: EmailMessage
    ) -> str:
        """
        Build the date to record for an oa / phone / technical email. Date-only.

        For these categories date_mentioned IS the scheduled event date, so prefer it.
        Falls back to the email's arrival date rather than datetime.now() so the value
        is a function of the email — re-running over the same message re-derives the
        same date instead of appending today's. Date-only because columns G/H/I carry
        a DATE, not DATE_TIME, number format.
        """
        mentioned = classification.date_mentioned
        if mentioned:
            candidate = normalize_date(str(mentioned))
            if parse_sheet_datetime(candidate):
                return candidate
            logger.warning(
                f"Unparseable date_mentioned {mentioned!r}; falling back to email date"
            )
        return self._local_naive(email.date).strftime("%m/%d/%Y")

    def _update_job_status(
        self,
        job: JobRow,
        classification: EmailClassification,
        email: EmailMessage
    ) -> bool:
        """
        Update job status based on email classification.

        Args:
            job: Job to update
            classification: Email classification
            email: Original email message

        Returns:
            True if updated successfully
        """
        category = classification.category

        # Skip unknown category
        if category == "unknown":
            return False

        new_status = CATEGORY_TO_STATUS.get(category)
        if not new_status:
            logger.warning(f"No status mapping for category: {category}")
            return False

        # Everything below this point writes to the row — status, the date helpers, the
        # notes append, the color, the Discord ping — so the staleness check has to come
        # before all of it, not just before the status write.
        if not self._is_newer_than_last_email(job, email):
            logger.info(
                f"Stale email for row {job.row_number} ({job.company}); "
                f"last recorded {job.last_email_time}, skipping"
            )
            self.stats["stale_skipped"] += 1
            return False

        updates = {}

        # Update status, unless doing so would walk the row backwards. Two systems often
        # mail within the same minute — an assessment invite from one vendor and the ATS
        # confirmation from another — and whichever lands second wins on timestamp alone.
        # Letting a confirmation reset a row already at OA loses real progress, so the
        # status is held and everything else about the email is still recorded.
        if self._is_status_regression(job.status, new_status):
            logger.info(
                f"Row {job.row_number} ({job.company}) is already {job.status}; "
                f"keeping it rather than moving back to {new_status}"
            )
            self.stats["status_regression_blocked"] += 1
        else:
            updates["status"] = new_status

        # Rides the same batch as status, so recording it costs no extra API call.
        updates["last_email_time"] = self._local_naive(email.date).strftime(
            "%m/%d/%Y %H:%M:%S"
        )
        # Written together with the time: the time says when the last mail landed, this
        # says what it was. Completed Stages records that an assessment was finished but
        # not that a newer one has since been sent, which is the gap this closes — an
        # "OA Invite" here on a row already carrying "OA" in W means one is done and
        # another is waiting.
        updates["last_event"] = event_label(classification.category)

        # Update relevant date column. The two paths differ: application_date is
        # single-valued and must come from the confirmation's arrival time — never from
        # date_mentioned, which the AI fills with any date in the body (often an OA or
        # offer deadline). OA/phone/tech dates are scheduled events that may
        # legitimately accumulate across rounds, so they keep the appending helper.
        if category == "confirmation":
            stamp = self._application_date_value(job, email)
            if stamp:
                updates["application_date"] = stamp
        elif category in CATEGORY_TO_DATE_COLUMN:
            self.sheets_client.add_date_to_column(
                job.row_number,
                CATEGORY_TO_DATE_COLUMN[category],
                self._event_date_value(classification, email),
            )

        # For offer/rejection, add details to notes
        if category in ("offer", "rejection"):
            note_parts = [f"Email received: {email.date.strftime('%Y-%m-%d')}"]
            if classification.key_details:
                note_parts.append(classification.key_details)
            if classification.action_required:
                note_parts.append(f"Action: {classification.action_required}")

            note = "; ".join(note_parts)
            self.sheets_client.append_to_notes(job.row_number, note)

        # Apply status update. When the status was held back the row keeps whatever it
        # already had, so the colour and the Discord ping must follow the status the row
        # actually ends up with — recolouring to a stage the row is not at, or announcing
        # a move that did not happen, is worse than the original overwrite.
        status_written = "status" in updates
        effective_status = new_status if status_written else job.status

        try:
            self.sheets_client.update_job(job.row_number, updates)
            if status_written:
                logger.info(f"Updated job status: {job.company} - {job.position} -> {new_status}")
            else:
                logger.info(
                    f"Recorded {category} mail on row {job.row_number} "
                    f"({job.company}) without changing status {job.status}"
                )

            # Apply row color based on status
            self.sheets_client.apply_status_color(job.row_number, effective_status)

            # Keep the correspondence for OA and later. The row records that a stage
            # happened; the email holds the assessment link, the deadline and the
            # instructions. Runs after the sheet write so no archive file exists for a
            # row that was not actually updated, and never raises: losing a status
            # update to a disk error would be a far worse trade.
            self._archive_status_email(job, category, email)

            # Send Discord notification if dream company
            if status_written and self.config.discord.enabled and job.company:
                if is_dream_company(job.company, self.config.user.target_companies):
                    logger.info(f"  Dream company status change! Sending Discord notification...")
                    try:
                        notify_status_change(job.company, job.position, new_status, job.position_url or "")
                    except Exception as discord_error:
                        logger.warning(f"  Failed to send Discord notification: {discord_error}")

            return True
        except Exception as e:
            logger.error(f"Failed to update job: {e}")
            return False

    def _record_stage_done(
        self,
        client: GmailClient,
        email: EmailMessage,
        classification: EmailClassification,
    ) -> bool:
        """
        Mark a finished stage on the one row that can be sitting at it.

        These emails come from the assessment platform, not the employer — "Assessment
        completed: Susquehanna Coding Assessment" — so they name a company and almost
        never a position. The stage itself does the narrowing instead: only a row whose
        status is that stage can have just finished it.

        Marking *every* qualifying row was considered, because Millennium runs one
        assessment across two roles. It is not safe: SIG issues a separate assessment per
        position, so the same rule would record work that has not been done. When more
        than one row qualifies the email is flagged for review and nothing is written —
        `scripts/mark_stage_done.py` is the intended route for those.

        Never changes the status, and never depends on it either. Rows 261, 262, 315 and
        317 are all Rejected with an OA Date — the assessments were taken, the outcome
        simply arrived afterwards. Matching on "is at OA" would have refused all four, so
        the test is whether the row *reached* the stage.
        """
        stage = classification.stage_completed

        candidates: List[JobRow] = []
        company_used = ""
        for company in classification.company_candidates or []:
            rows = self.sheets_client.find_jobs_by_company(company)
            if rows:
                company_used = company
                candidates = [r for r in rows if r.row_number not in self._struck_rows]
                break

        at_stage = [r for r in candidates if reached_stage(r, stage)]

        if len(at_stage) != 1:
            ambiguous = len(at_stage) > 1
            reason = REASON_AMBIGUOUS if ambiguous else REASON_UNTRACKED
            logger.warning(
                f"{stage} completion from {email.sender_email}: "
                f"{len(at_stage)} row(s) of '{company_used or '?'}' reached {stage} — "
                f"not guessing; flagged for review"
            )
            self.stats["no_match"] += 1
            if ambiguous:
                self.stats["ambiguous"] += 1
            self._log_needs_review(
                client, email, classification,
                MatchOutcome(reason=reason, company=company_used, candidates=at_stage),
            )

            # A completion that found several rows at the stage needs a person: more
            # runs will not resolve it, so consume it as before and let the review log
            # carry it.
            #
            # A completion that found *none* is a different thing — usually a race. The
            # invite that puts the row at this stage may not have landed yet, and a
            # receipt can arrive minutes behind it. Marking this processed retires the
            # email permanently: the row reaches the stage a moment later and nothing
            # ever comes back for it, which is how an assessment that was genuinely
            # taken ends up recorded only by hand. Leave it unprocessed so the next run
            # tries again. The retry is bounded without any extra state, because the
            # fetch window is `after:` GMAIL_LOOKBACK_DAYS — once the email falls out
            # of that window it stops being fetched at all.
            if ambiguous:
                client.mark_as_processed(email.message_id)
            else:
                logger.info(
                    f"Leaving {stage} completion from {email.sender_email} unprocessed; "
                    f"a later run can record it once a row reaches {stage}"
                )
            return False

        job = at_stage[0]
        updated = add_stage(job.completed_stages, stage)
        event = event_label(classification.category)
        arrived = self._local_naive(email.date)
        recorded = parse_sheet_datetime(job.last_email_time)

        # Nothing to do only when all three columns would keep their values. Completed
        # Stages alone is not enough: it dedupes, so a company's second assessment adds
        # nothing there — returning early on that comparison left Capital One's receipt
        # unrecorded, since the row already said "OA". Last Email Time counts too, or a
        # later completion would be seen and then not recorded as having arrived.
        if (updated == job.completed_stages
                and event == (job.last_event or "")
                and recorded is not None and arrived <= recorded):
            logger.info(
                f"{stage} already recorded on row {job.row_number} ({job.company})"
            )
            client.mark_as_processed(email.message_id)
            return False

        try:
            # Last Email Time is the most recent email to touch this row, whatever it
            # was, so a completion writes it like anything else.
            #
            # It was left out at first to protect the staleness guard: this column is
            # what stops an older email changing a status, and a completion is not a
            # status change. That weighed a rare case — an email both delivered late and
            # dated before the completion — against a constant one, the column
            # understating how recently a row was touched every single time. Row 518 read
            # as last touched on 4 August when its receipt had arrived that evening.
            self.sheets_client.update_job(job.row_number, {
                "completed_stages": updated,
                "last_event": event,
                "last_email_time": arrived.strftime("%m/%d/%Y %H:%M:%S"),
            })
            logger.info(
                f"Marked {stage} done on row {job.row_number}: {job.company} - {job.position}"
            )
            self.stats["stages_completed"] += 1
            self.stats["updated"] += 1
        except Exception as e:
            logger.error(f"Failed to record {stage} completion on row {job.row_number}: {e}")
            return False

        client.mark_as_processed(email.message_id)
        return True

    def _process_email(self, client: GmailClient, email: EmailMessage) -> bool:
        """
        Process a single email.

        Args:
            client: The Gmail client (account) this email came from
            email: Email message to process

        Returns:
            True if job was updated, False otherwise
        """
        # Log email details for verification
        date_str = email.date.strftime("%Y-%m-%d %H:%M")
        logger.info(f"Processing [{client.label}]: {email.sender_email} | {date_str} | [{email.category}] {email.subject}")

        # Apply privacy filters (Layer 2 & 3)
        passed, reason = apply_privacy_filters(email)
        if not passed:
            logger.debug(f"Email filtered: {reason}")
            self.stats["filtered_out"] += 1
            # Still mark as processed to avoid re-checking
            client.mark_as_processed(email.message_id)
            return False

        # Classify with AI
        classification = self.classifier.classify(email)
        if classification is None:
            logger.warning("Classification failed")
            client.mark_as_processed(email.message_id)
            return False

        self.stats["classified"] += 1
        logger.info(f"AI extracted - Category: {classification.category}, Companies: {classification.company_candidates}, Position: {classification.position}")

        # "unknown" is everything that fits no stage: candidate portal invites, profile
        # setups, event invites — and outright junk. It never writes a status, and as of
        # 2026-08-02 it no longer writes anything else either.
        #
        # It used to match against the sheet and leave a note on any row already applied
        # to, on the theory that a company candidate could only come from real
        # correspondence. That theory was wrong: a newsletter about Gemini Robotics
        # yields "Google", matches the applied Google row, and writes a note there. Row
        # 304 collected four such notes, one of them ("Invitation to coffee chat with
        # Jeff") invented from an AI newsletter — the row asserted something that never
        # happened. Filtering bulk senders would have caught the newsletters but not a
        # transactional "you shared data with myworkday.com" notice, so the matching is
        # dropped altogether rather than guarded.
        if classification.category == "unknown":
            self.stats["unknown_category"] += 1
            logger.debug(
                f"Unknown category from {email.sender_email}; not matched to any row"
            )
            client.mark_as_processed(email.message_id)
            return False

        # A completion is not a stage change, so it takes its own path: it never writes
        # a status, and it is matched by which rows are sitting at that stage rather
        # than by position text, which these emails rarely carry.
        if classification.category == "stage_done":
            return self._record_stage_done(client, email, classification)

        # Find matching job
        outcome = self._find_matching_job(classification, email)
        if not outcome.matched:
            self.stats["no_match"] += 1
            if outcome.reason == REASON_AMBIGUOUS:
                self.stats["ambiguous"] += 1
            self._log_needs_review(client, email, classification, outcome)
            client.mark_as_processed(email.message_id)
            return False

        job = outcome.job

        self.stats["matched"] += 1
        logger.debug(f"Matched to: {job.company} - {job.position} (row {job.row_number})")

        # Matched, but out of rows that are copies of each other. The status below is
        # written normally; this only records that the twins exist, so the 9am/5pm
        # digest can surface them and mark_canonical.py can collapse them.
        if outcome.duplicates:
            rows = ", ".join(str(d.row_number) for d in outcome.duplicates)
            logger.info(f"  Row {job.row_number} is one of {len(outcome.duplicates)} duplicate rows: {rows}")
            self.stats["duplicate_rows"] += 1
            self._log_needs_review(
                client,
                email,
                classification,
                MatchOutcome(
                    reason=REASON_DUPLICATE_ROWS,
                    company=job.company,
                    candidates=outcome.duplicates,
                ),
            )

        # Update job status
        updated = self._update_job_status(job, classification, email)
        if updated:
            self.stats["updated"] += 1

        # Mark as processed
        client.mark_as_processed(email.message_id)

        return updated

    def run(self) -> dict:
        """
        Run the Gmail checking pipeline.

        Returns:
            Dict with statistics about the run
        """
        start_time = time.time()
        logger.info("=" * 60)
        logger.info("Starting Gmail status check")
        logger.info("=" * 60)

        # Reset stats
        self.stats = {k: 0 for k in self.stats}

        # This process may be the only one touching the sheet, so it cannot rely on
        # scrape_jobs.py to have created the Last Email Time column. Idempotent per
        # client, so the scheduled loop pays for it once. Never fatal — a missing header
        # only costs formatting, and the writes below still land.
        try:
            self.sheets_client.ensure_headers()
        except Exception as e:
            logger.warning(f"Could not verify sheet headers: {e}")

        # Struck-through rows are retired: the matcher skips them, which is how an
        # otherwise ambiguous company narrows to one live row. Never fatal — losing
        # this only costs tie-breaking, so the run continues without it.
        try:
            self._struck_rows = self.sheets_client.get_struck_rows()
            if self._struck_rows:
                logger.info(f"Skipping {len(self._struck_rows)} struck-through row(s)")
        except Exception as e:
            logger.warning(f"Could not read struck-through rows, treating all as live: {e}")
            self._struck_rows = set()

        # Fetch recent emails from every configured account
        accounts = ", ".join(c.label for c in self.gmail_clients)
        logger.info(f"Fetching emails from last {self.config.gmail_lookback_days} day(s) for: {accounts}")
        if self.reprocess:
            logger.info("Reprocess mode: ignoring processed email caches")

        fetched = []  # (client, email) pairs
        for client in self.gmail_clients:
            try:
                emails = client.fetch_recent_emails(skip_processed=not self.reprocess)
            except Exception as e:
                # One broken account (expired token, wrong auth) must not stop the others
                logger.error(f"Failed to fetch emails for {client.label}: {e}")
                continue

            self.stats["accounts_checked"] += 1
            fetched.extend((client, email) for email in emails)
            logger.info(f"[{client.label}] Found {len(emails)} new emails to process")

            # Log summary of all fetched emails
            if emails:
                logger.info("-" * 60)
                logger.info(f"Emails found ({client.label}):")
                for i, email in enumerate(emails, 1):
                    date_str = email.date.strftime("%Y-%m-%d %H:%M")
                    subject_preview = email.subject[:50] + "..." if len(email.subject) > 50 else email.subject
                    logger.info(f"  {i}. [{email.category}] {email.sender_email}")
                    logger.info(f"     {date_str} | {subject_preview}")
                logger.info("-" * 60)

        self.stats["emails_fetched"] = len(fetched)

        # Oldest first. Gmail returns newest-first, and the staleness guard rejects
        # anything not strictly newer than the row's Last Email Time — so in that order
        # a batch would apply only its first email per row and drop the rest, losing the
        # OA date from a confirmation -> OA -> rejection sequence that arrived together.
        fetched.sort(key=lambda pair: self._local_naive(pair[1].date))

        # Process each email
        for client, email in fetched:
            try:
                self._process_email(client, email)
            except Exception as e:
                logger.error(f"Error processing email '{email.subject[:40]}...': {e}")
                # Still mark as processed to avoid infinite retries
                client.mark_as_processed(email.message_id)

        # Log summary
        elapsed = time.time() - start_time
        logger.info("=" * 60)
        logger.info("Gmail check complete!")
        logger.info(f"  Time elapsed: {elapsed:.1f}s")
        logger.info(f"  Accounts checked: {self.stats['accounts_checked']}/{len(self.gmail_clients)}")
        logger.info(f"  Emails fetched: {self.stats['emails_fetched']}")
        logger.info(f"  Filtered out: {self.stats['filtered_out']}")
        logger.info(f"  Classified: {self.stats['classified']}")
        logger.info(f"  Unknown category: {self.stats['unknown_category']}")
        logger.info(f"  Matched to jobs: {self.stats['matched']}")
        logger.info(f"  No match found: {self.stats['no_match']} (ambiguous: {self.stats['ambiguous']})")
        logger.info(f"  Stale (older than last email): {self.stats['stale_skipped']}")
        logger.info(f"  Status held (would regress): {self.stats['status_regression_blocked']}")
        logger.info(f"  Stages marked done: {self.stats['stages_completed']}")
        logger.info(f"  Jobs updated: {self.stats['updated']}")
        logger.info("=" * 60)

        return self.stats


def run_once(reprocess: bool = False):
    """Run the pipeline once."""
    config = get_config()
    setup_logging("gmail", config, console=True)

    checker = GmailChecker(config, reprocess=reprocess)
    stats = checker.run()

    return stats


def authorize_accounts() -> int:
    """
    Interactively authorize every configured Gmail account.

    Background services run headless and cannot open a consent screen, so each
    account needs its OAuth token created once from a terminal.
    """
    config = get_config()
    setup_logging("gmail", config, console=True)

    clients = get_gmail_clients()
    failures = 0

    for client in clients:
        print()
        print("=" * 60)
        print(f"Authorizing: {client.label}")
        print(f"Token file:  {client.token_path}")
        if client.token_path.exists():
            print("Existing token found - reusing it (delete the file to re-authorize).")
        else:
            print("A browser window will open. Sign in with THIS account.")
        print("=" * 60)

        try:
            print(f"  OK - authorized as {client.authorized_email()}")
        except Exception as e:
            print(f"  FAILED - {e}")
            failures += 1

    print()
    print(f"Authorized {len(clients) - failures}/{len(clients)} account(s)")
    return 1 if failures else 0


def run_scheduled():
    """Run the pipeline on a schedule."""
    config = get_config()
    setup_logging("gmail", config, console=True)

    interval = config.gmail_check_interval_minutes
    logger.info(f"Starting scheduled Gmail checker (every {interval} minutes)")

    scheduler = BlockingScheduler()

    def job():
        try:
            checker = GmailChecker(config)
            checker.run()
        except Exception as e:
            logger.error(f"Scheduled job failed: {e}")

    # Run immediately on start
    job()

    # Schedule recurring runs
    scheduler.add_job(job, 'interval', minutes=interval)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


def main():
    parser = argparse.ArgumentParser(description="ApplyPotato Gmail Status Checker")
    parser.add_argument("--scheduled", action="store_true", help="Run on schedule")
    parser.add_argument("--reprocess", action="store_true", help="Reprocess all emails (ignore processed_emails.json)")
    parser.add_argument("--auth", action="store_true", help="Authorize each configured Gmail account (one-time, interactive)")
    args = parser.parse_args()

    if args.auth:
        raise SystemExit(authorize_accounts())

    if args.scheduled:
        run_scheduled()
    else:
        run_once(reprocess=args.reprocess)


if __name__ == "__main__":
    main()
