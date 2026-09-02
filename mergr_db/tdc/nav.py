"""
TDC — the shape of the section.

Two halves: Dispatch is who receives the publication, Deals is what it publishes.
Kept as data rather than hard-coded into templates so that `built` stays honest —
an unbuilt screen renders its own placeholder, and finishing one means flipping a
flag here rather than remembering to remove a notice somewhere else.
"""

HALVES = [
    {"key": "dispatch", "label": "Dispatch", "href": "/tdc/dispatch/subscribers",
     "blurb": "Who receives it — subscribers, deliverability, and the send itself."},
    {"key": "deals", "label": "Deals", "href": "/tdc/deals",
     "blurb": "What it publishes — sourcing, the record, entities, and shipping to the site."},
]

DISPATCH = [
    {"key": "subscribers", "label": "Subscribers", "href": "/tdc/dispatch/subscribers",
     "built": True,
     "blurb": "Sign-ups, consent, and which firms they come from."},
    {"key": "deliverability", "label": "Deliverability", "href": "/tdc/dispatch/deliverability",
     "built": True,
     "blurb": "Bounces, complaints, and the sandbox queue."},
    {"key": "sends", "label": "Sends", "href": "/tdc/dispatch/sends",
     "built": False,
     "blurb": "Compose an issue and send it to the mailable list.",
     "will": ["Compose against the deals published since the last send",
              "Recipients resolved at send time — confirmed and deliverable only",
              "List-Unsubscribe and List-Unsubscribe-Post headers (RFC 8058)",
              "A record of every send: who it went to, what bounced"],
     "why": "Nothing has ever been sent. The list is real and the headers matter more than "
            "the template does, so this is the piece that decides whether the first issue "
            "lands in an inbox or a spam folder."},
]

DEALS = [
    {"key": "queue", "label": "Queue", "href": "/tdc/deals",
     "built": False,
     "blurb": "Everything in flight, by status.",
     "will": ["Deals ordered by what needs attention, not by date",
              "Status through the pipeline: source, extracted, verified, drafted, edited, review",
              "What is blocked and on what — a missing source, an unresolved party"],
     "why": "The working list. Until deals exist there is nothing to queue."},
    {"key": "entities", "label": "Entities", "href": "/tdc/deals/entities",
     "built": False,
     "blurb": "Resolution against Mergr, and the matches too weak to accept.",
     "will": ["Party names resolved by domain, then legal name, then name and sector",
              "A review queue for low-confidence matches — never the closest name",
              "The Mergr bridge: 5,026 firms and 191,088 companies already in this database",
              "Unresolved is a valid state; wrong is not"],
     "why": "The first thing worth building, and the only part that gets more expensive by "
            "waiting. Party names in the ten published records are still plain strings, so "
            "there is currently no entity for the two halves of TDC to meet at."},
    {"key": "sources", "label": "Sources", "href": "/tdc/deals/sources",
     "built": False,
     "blurb": "Fetched documents, with their text stored.",
     "will": ["Every source stored as text, because link rot is certain",
              "Primary or secondary decided per field, not per source",
              "Mergr appears here as a lead and can never become a citation"],
     "why": "The evidence layer. Claims point at spans inside these documents, so the text "
            "has to outlive the URL."},
    {"key": "publish", "label": "Publish", "href": "/tdc/deals/publish",
     "built": False,
     "blurb": "Approved record to JSON, build, ship to S3.",
     "will": ["Export approved deals to content/deals/*.json",
              "Run the static build and sync to S3",
              "Invalidate CloudFront and confirm the page is live"],
     "why": "Today this runs by hand from a terminal, which means the site can only be "
            "published by someone with the repo checked out."},
]

def by_key(items, key):
    for it in items:
        if it["key"] == key:
            return it
    return {}
