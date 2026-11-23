"""`export --format json`: the versioned snapshot bundle."""

from __future__ import annotations

import json

FORMAT = "json"
ORDER = 10


def render(project, od, bundle, snapshots, events) -> str:
    from ..paths import scrub

    return json.dumps(scrub(bundle, project), indent=2, sort_keys=True) + "\n"
