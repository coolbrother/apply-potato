#!/usr/bin/env python3
"""
Test script for AI job extraction.

Usage:
    python test_ai_extractor.py                    # Test all files in scraped_content/
    python test_ai_extractor.py --file <path>      # Test specific file
    python test_ai_extractor.py --save             # Save extracted JSON to extracted_jobs/
    python test_ai_extractor.py --count 2          # Only process first N files
"""

import argparse
import json
import sys
import io
import time
from pathlib import Path
from dataclasses import asdict

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_config
from src.logging_config import setup_logging
from src.ai_extractor import AIExtractor, ExtractedJob


def format_job(job: ExtractedJob) -> str:
    """Format extracted job for display."""
    lines = [
        "=" * 60,
        f"Company: {job.company}",
        f"Title: {job.title}",
        f"Job Type: {job.job_type}",
        f"Work Model: {job.work_model} (Remote: {job.is_remote})",
        f"Locations: {', '.join(job.locations) if job.locations else 'N/A'}",
        "-" * 40,
        f"Salary: {job.salary_min} - {job.salary_max} {job.salary_period or ''} {job.currency or ''}".strip(),
        f"Category: {job.job_category}",
        "-" * 40,
        f"Class Standing: {job.class_standing_requirement}",
        f"Graduation Timeline: {job.graduation_timeline}",
        f"Season/Year: {job.season_year}",
        f"Work Authorization: {job.work_authorization}",
        f"Sponsorship Available: {job.sponsorship_available}",
        f"GPA Requirement: {job.gpa_requirement}",
    ]

    if job.degree_requirement:
        lines.append(f"Degree Requirement: {job.degree_requirement.level} ({job.degree_requirement.type})")
    else:
        lines.append("Degree Requirement: None")

    lines.extend([
        "-" * 40,
        f"Required Skills: {', '.join(job.required_skills[:5])}{'...' if len(job.required_skills) > 5 else ''}",
        f"Preferred Skills: {', '.join(job.preferred_skills[:5])}{'...' if len(job.preferred_skills) > 5 else ''}",
        f"Required Majors: {', '.join(job.required_majors) if job.required_majors else 'N/A'}",
        "-" * 40,
        f"Posted: {job.posted_date}",
        f"Deadline: {job.deadline}",
        f"Apply URL: {job.apply_url[:60] + '...' if job.apply_url and len(job.apply_url) > 60 else job.apply_url}",
        "-" * 40,
        f"Summary: {job.description_summary[:200] + '...' if job.description_summary and len(job.description_summary) > 200 else job.description_summary}",
        "=" * 60,
    ])

    return "\n".join(lines)


def job_to_dict(job: ExtractedJob) -> dict:
    """Convert ExtractedJob to dict, excluding raw_response."""
    data = asdict(job)
    # Remove raw_response as it's too large
    data.pop("raw_response", None)
    return data


def main():
    parser = argparse.ArgumentParser(description="Test AI job extraction")
    parser.add_argument("--file", type=str, help="Specific file to test")
    parser.add_argument("--save", action="store_true", help="Save extracted JSON to extracted_jobs/")
    parser.add_argument("--count", type=int, default=0, help="Only process first N files (0 = all)")
    args = parser.parse_args()

    # Load config and setup logging
    config = get_config()
    setup_logging("extractor_test", config, console=True)

    print(f"\n{'='*60}")
    print(f"AI Extractor Test")
    print(f"Provider: {config.ai_provider}")
    print(f"Model: {config.openai_model if config.ai_provider == 'openai' else config.gemini_model}")
    print(f"{'='*60}\n")

    # Create extractor
    extractor = AIExtractor(config)

    # Find files to process
    scraped_dir = Path(__file__).parent.parent / "scraped_content"

    if args.file:
        files = [Path(args.file)]
    elif scraped_dir.exists():
        files = sorted(scraped_dir.glob("*.txt"))
        if args.count > 0:
            files = files[:args.count]
    else:
        print(f"ERROR: scraped_content/ directory not found")
        print(f"Run test_scraper.py --save first to generate test content")
        return 1

    if not files:
        print("No files found to process")
        return 1

    print(f"Found {len(files)} file(s) to process\n")

    # Create output directory if saving
    output_dir = Path(__file__).parent.parent / "extracted_jobs"
    if args.save:
        output_dir.mkdir(exist_ok=True)
        print(f"Saving extracted jobs to: {output_dir}\n")

    # Process each file
    success_count = 0
    fail_count = 0
    total_time = 0

    for file_path in files:
        print(f"\n>>> Processing: {file_path.name}")
        print(f"    Size: {file_path.stat().st_size:,} bytes")

        # Read content
        content = file_path.read_text(encoding="utf-8")
        print(f"    Content length: {len(content):,} chars")

        # Extract
        start_time = time.time()
        # Don't pass source_url - in production this comes from GitHub parser
        # For testing, apply_url will be whatever AI extracts from the page
        jobs = extractor.extract(content, source_url="")
        elapsed = time.time() - start_time
        total_time += elapsed

        print(f"    Extraction time: {elapsed:.2f}s")
        print(f"    Extracted {len(jobs)} job(s)")

        if jobs:
            for idx, job in enumerate(jobs):
                success_count += 1
                if len(jobs) > 1:
                    print(f"\n    [Position {idx + 1}/{len(jobs)}]")
                print(f"\n{format_job(job)}")

                # Save if requested
                if args.save:
                    if len(jobs) > 1:
                        output_file = output_dir / f"{file_path.stem}_{idx + 1}.json"
                    else:
                        output_file = output_dir / f"{file_path.stem}.json"
                    with open(output_file, "w", encoding="utf-8") as f:
                        json.dump(job_to_dict(job), f, indent=2)
                    print(f"\n    Saved to: {output_file.name}")
        else:
            fail_count += 1
            print(f"    FAILED: Could not extract job data")

    # Summary
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"Total files: {len(files)}")
    print(f"Successful: {success_count}")
    print(f"Failed: {fail_count}")
    print(f"Total time: {total_time:.2f}s")
    print(f"Avg time per file: {total_time / len(files):.2f}s")
    print(f"{'='*60}\n")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
