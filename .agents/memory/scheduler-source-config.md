---
name: Scheduler and source configuration
description: Durable constraints for the automatic polling loop and source-target loading.
---

Empty environment variables are common in the deployment environment and must be treated as unset so typed defaults, especially the 900-second polling interval, remain usable.

The automatic scheduler owns `next_poll_at`. A manual Discord scan may run immediately, but it must preserve the scheduler's existing next automatic deadline rather than shifting or replacing it.

Source configuration must fail explicitly when JSON is malformed or contains invalid targets. When `SOURCE_TARGETS_JSON` is empty, the configured JSON file is the required fallback; returning an empty source list hides deployment regressions.

**Why:** Empty typed settings previously prevented application startup, and manual scans could make the displayed automatic schedule inaccurate.

**How to apply:** Preserve these rules when changing settings parsing, startup lifespan, worker scheduling, or Discord scan commands.