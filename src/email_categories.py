"""
The email categories and their classification rules, read from one file.

prompts/email_categories.txt is the single source of the rules for both email
classifiers. The LLM classifier gets them rendered into its prompt (render_llm); Jev gets
each category's text as that label's description (jev_criteria). Keeping one copy is
what makes the two comparable: when a rule changes, both models see the change.

CATEGORIES is also the single source of the category names in code — the LLM's response
validation and Jev's labels both use it — and the file must describe exactly these.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

CATEGORIES_FILENAME = "email_categories.txt"

# Every category a status email can fall into, in the order the prompt lists them.
CATEGORIES = ("confirmation", "oa", "phone", "technical", "stage_done", "offer",
              "rejection", "withdrawn", "unknown")

_SECTION = re.compile(r"^\[(\w+)\]\s*$")


@dataclass(frozen=True)
class Category:
    """One category: its one-line summary and its detailed rules (may be empty)."""
    name: str
    summary: str
    rules: str

    def description(self) -> str:
        """Summary and rules together — the whole of what this category means."""
        return f"{self.summary}\n\n{self.rules}" if self.rules else self.summary


def section_order(path: Path) -> Tuple[str, ...]:
    """
    The file's section names in the order written, [guidance] included.

    The LLM prompt's rules follow this order rather than CATEGORIES: placement in a
    prompt changes what a model does — moving the stage_done rules after the invitation
    categories made nano read interview invitations as completions — so the file
    decides where each block goes.
    """
    return tuple(m.group(1) for line in path.read_text(encoding="utf-8").splitlines()
                 if (m := _SECTION.match(line)))


def load_categories(path: Path) -> Tuple[str, Dict[str, Category]]:
    """
    Read the categories file into (guidance, {name: Category}), the categories in
    CATEGORIES order whatever order the file writes them in.

    Raises:
        ValueError: No [guidance], categories other than exactly CATEGORIES, or a
            category with no summary. A missing or misspelt label would otherwise
            silently change what the models are allowed to answer.
    """
    sections: Dict[str, list] = {}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            continue
        m = _SECTION.match(line)
        if m:
            current = m.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    text = {name: "\n".join(lines).strip() for name, lines in sections.items()}

    guidance = text.pop("guidance", "")
    if not guidance:
        raise ValueError(f"{path.name}: no [guidance] section")
    if sorted(text) != sorted(CATEGORIES) or len(sections) != len(CATEGORIES) + 1:
        raise ValueError(f"{path.name}: categories {list(text)} must be exactly {list(CATEGORIES)}")

    categories = {}
    for name in CATEGORIES:
        body = text[name]
        summary, _, rules = body.partition("\n\n")
        if not summary.strip():
            raise ValueError(f"{path.name}: [{name}] has no summary")
        categories[name] = Category(name, summary.strip(), rules.strip())
    return guidance, categories


def render_llm(template: str, guidance: str, categories: Dict[str, Category],
               order: Tuple[str, ...] = ()) -> str:
    """
    Fill the LLM prompt's category placeholders.

      {category_names}  "confirmation | oa | ... | unknown", for the JSON schema line
      {category_list}   one bullet per category with its summary, in CATEGORIES order
      {category_rules}  the guidance and each category's rules under its name, in the
                        file's order (`order`, from section_order); guidance then the
                        categories when no order is given
    """
    def bullet(cat: Category) -> str:
        first, *rest = cat.summary.splitlines()
        return "\n".join([f'- "{cat.name}": {first}'] + [f"  {line}" for line in rest])

    blocks = {"guidance": guidance}
    blocks.update({c.name: f"{c.name}:\n{c.rules}" for c in categories.values() if c.rules})
    names = " | ".join(categories)
    listing = "\n".join(bullet(c) for c in categories.values())
    rules = "\n\n".join(blocks[name] for name in (order or blocks) if name in blocks)
    return (template.replace("{category_names}", names)
                    .replace("{category_list}", listing)
                    .replace("{category_rules}", rules))


def jev_criteria(categories: Dict[str, Category]) -> Dict[str, str]:
    """Jev's label → description map: each category's summary and rules."""
    return {name: cat.description() for name, cat in categories.items()}
