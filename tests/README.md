# Running Tests

## Quick Start

```bash
# Activate virtual environment
venv\Scripts\activate  # Windows
source venv/bin/activate  # macOS/Linux

# Run all tests
pytest tests/ -v

# Save results to file
pytest tests/ -v > logs/test_results.txt 2>&1
```

## Run Specific Tests

```bash
# Run a single test file
pytest tests/test_filters.py -v
pytest tests/test_scoring.py -v
pytest tests/test_deduplication.py -v
pytest tests/test_notifications.py -v
pytest tests/test_integration.py -v

# Run tests matching a pattern
pytest tests/ -v -k "dream_company"
pytest tests/ -v -k "graduation"

# Run a specific test function
pytest tests/test_notifications.py::TestIsDreamCompany::test_exact_match -v
```

## Test Structure

```
tests/
├── conftest.py              # Pytest fixtures and configuration
├── mocks/
│   └── mock_sheets.py       # In-memory Google Sheets mock
├── fixtures/
│   ├── test_config.json     # Test user profile
│   ├── github_markdown/     # Mock GitHub job listings
│   ├── job_pages/           # HTML fixtures for AI extraction
│   └── emails/              # JSON email fixtures
├── test_deduplication.py    # URL normalization tests
├── test_filters.py          # Hard eligibility filter tests
├── test_scoring.py          # Soft fit scoring tests
├── test_notifications.py    # Dream company matching tests
├── test_integration.py      # GitHub parser + mock sheets tests
├── test_ai_email.py         # Email classification (real AI calls)
└── test_ai_extraction.py    # Job extraction (real AI calls)
```

## Test Categories

### Unit Tests (No API keys required)
- `test_deduplication.py` - URL normalization
- `test_filters.py` - Class standing, graduation timeline, work auth filters
- `test_scoring.py` - Major, GPA, location, skills scoring
- `test_notifications.py` - Dream company fuzzy matching
- `test_integration.py` - GitHub parser, mock sheets

### AI Tests (Require API keys)
- `test_ai_email.py` - Requires OPENAI_API_KEY or GEMINI_API_KEY
- `test_ai_extraction.py` - Requires OPENAI_API_KEY or GEMINI_API_KEY

## Options

```bash
# Verbose output
pytest tests/ -v

# Show print statements
pytest tests/ -v -s

# Stop on first failure
pytest tests/ -v -x

# Run only failed tests from last run
pytest tests/ -v --lf

# Show slowest tests
pytest tests/ -v --durations=5
```

## Expected Output

All 43 tests should pass:
```
tests/test_ai_email.py::TestAIEmailClassification::test_classifies_confirmation_email PASSED
tests/test_ai_email.py::TestAIEmailClassification::test_classifies_rejection_email PASSED
...
====================== 43 passed in 22.65s =======================
```

## Known Warnings

The httplib2 deprecation warnings are from an external library and don't affect functionality:
```
DeprecationWarning: 'setName' deprecated - use 'set_name'
```
These will be resolved when httplib2 releases an update.
