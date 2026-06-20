---
description: Classify the most recent buy-flow failure and recommend the next step.
---
Run `roomieorder failures`. Take the most recent trouble row and `Read` its
newest screenshot (and `*_dom.html` / `*_probe.txt` if present).

Classify the failure using AGENTS.md §1–§3: selector drift, logged-out /
sign-in wall, CAPTCHA/OTP challenge, or an outright Akamai block. State which
stage died (from the shot tag) and recommend exactly one next command — e.g.
`dump-dom`, `verify-selectors`, `login`, or `resume`.

Do not order or log in yourself; just diagnose and recommend.
