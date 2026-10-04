"""Questions about the database itself that are answered from the index, with no AI call.

"What tables do I have?" is one of the first things a new user asks, and the answer is
already in schema_elements. Recognising it with a few patterns saves a Gemini request. Other
schema questions ("which table has customer emails?") go to the model, which answers them
from the documents in its prompt (reply body "-- Answer:").
"""
import re

_TABLE_LISTING = re.compile(
    r"^\s*(?:"
    r"(?:what|which)\s+(?:tables|data(?:sets)?|views)\s+(?:do|does|are|is)\b"
    r"|(?:list|show)(?:\s+me)?(?:\s+all)?(?:\s+the|\s+my)?\s+(?:tables|views|schema|database tables)\b"
    r"|what(?:'s| is)\s+in\s+(?:my|this|the)\s+(?:database|db|schema)\b"
    r"|(?:describe|explain)\s+(?:my|this|the)\s+(?:database|db|schema)\b"
    r"|what\s+can\s+i\s+ask\b"
    r")",
    re.IGNORECASE,
)
MAX_LISTED = 60


def is_table_listing(question: str) -> bool:
    return bool(_TABLE_LISTING.match(question))


def describe_tables(table_names: list[str]) -> str:
    """A plain-English list of the indexed tables and views."""
    if not table_names:
        return "I haven't found any tables yet. Re-index this connection, then ask again."
    names = sorted(table_names, key=str.lower)
    shown = ", ".join(names[:MAX_LISTED])
    more = f", and {len(names) - MAX_LISTED} more" if len(names) > MAX_LISTED else ""
    noun = "table or view" if len(names) == 1 else "tables and views"
    return (
        f"Your database has {len(names)} {noun}: {shown}{more}. "
        "Ask about any of them, for example how many rows one has or what changed this month."
    )
