---
description: Make this app survive dependency failures by hardening its weakest functions (ras-anything)
argument-hint: "[command that runs the app, e.g. python app.py]"
---

Use the ras-anything skill to harden this project's reliability. The app runs with: $ARGUMENTS

If no command was given, find out how the app is run (README, __main__, pyproject scripts). Only ask if it's truly unclear.
At the end, report availability under faults before → after (verify and bench), what the hardened code does
differently (timeouts, retries, idempotency, fallback), and how to undo (`ras-anything disable <id>`).
