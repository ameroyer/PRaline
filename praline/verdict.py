"""What a finished review *is*, independent of how it was produced.

The verdict vocabulary, the flattening of Claude's reply into postable items,
and the counts derived from them. Pure functions over plain dicts, with no
imports of their own — reviewer, auto, cli and slack all read a review through
this module, so none of them has to import another just to describe one.
"""

# PRaline's verdict on the PR as a whole, asked for in the review prompt and
# shown everywhere a review is reported: the CLI, the auto-mode summary, Slack.
STATUS_LABEL = {
    "ready": "\u2705 Ready to approve",
    "minor": "\U0001f6e0\ufe0f Needs minor revisions",
    "wip": "\U0001f6a7 Work in progress",
}
UNKNOWN_STATUS = "\u2754 No status given"


def status_key(review: dict) -> str:
    """Normalize the model's status to one of STATUS_LABEL's keys.

    The prompt asks for a single word, but a model that answers "needs minor
    revisions" in full is saying the right thing — read it rather than drop it."""
    raw = str(review.get("status", "")).strip().lower()
    if not raw:
        return ""
    if raw in STATUS_LABEL:
        return raw
    if "minor" in raw or "revision" in raw:
        return "minor"
    if "progress" in raw or "wip" in raw or "draft" in raw:
        return "wip"
    if "ready" in raw or "approve" in raw:
        return "ready"
    return ""


def status_label(review: dict) -> str:
    """The status as one human-readable, emoji-prefixed line."""
    return STATUS_LABEL.get(status_key(review), UNKNOWN_STATUS)


def flatten_review(review: dict) -> list[dict]:
    """Claude's reply as one ordered list of postable items.

    The summary comes first (it becomes the top-level PR comment), then replies
    to existing threads, then bugs, then the remaining comments. Both the
    interactive loop and auto mode consume this shape."""
    items = []
    if review.get("summary"):
        items.append({"file": None, "line": None, "severity": "overall", "body": review["summary"]})
    items += [{**r, "severity": "reply"} for r in review.get("replies", [])]
    items += [{**b, "severity": "bug"} for b in review.get("bugs", [])]
    items += list(review.get("comments", []))
    return items


def overview_of(review: dict, items: list[dict] | None = None) -> str:
    """The overall summary as it will actually be posted.

    Interactive users can edit or reject the summary in the approval loop, so
    prefer the accepted item's body and fall back to what the model wrote."""
    for item in items or []:
        if item.get("severity") == "overall":
            return item.get("body", "")
    return review.get("summary", "")


def reviewed_entry(pr, review: dict, items: list[dict]) -> dict:
    """One finished review as a dict, for the run summary and the Slack round-up.
    Both modes build it here so the two reports can never disagree."""
    return {
        "number": pr.number,
        "title": pr.title,
        "url": pr.url,
        "status": status_key(review) or "unknown",
        **count_items(items),
    }


def count_items(items: list[dict]) -> dict:
    """Tally postable items by kind — used for run summaries and Slack pings."""
    replies = [i for i in items if i.get("reply_to_id")]
    return {
        "comments_added": len(items) - len(replies),
        "comments_left": len(replies),
        "comments_resolved": sum(1 for i in replies if i.get("resolved")),
    }
