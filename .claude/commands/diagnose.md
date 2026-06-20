---
description: Read-only health check + recent failures, then summarize.
---
Run `roomieorder doctor` and `roomieorder failures`.

Summarize the system's health in a few lines: call out every `warn`/`FAIL`
from doctor, and the most recent failure row with its screenshot path. If a
screenshot or `*_dom.html` is listed and looks relevant, `Read` it. Use
AGENTS.md §0 for what each status and shot tag means.

Do not place orders, log in, or change config — these are read-only checks.
