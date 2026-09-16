# Eval case 2: Jira comment on an integration ticket

A status comment of the kind written on a client integration ticket, with client names, systems and keys replaced by placeholders (ACME, the ERP, the event bus, TICKET-42). Tells planted on purpose. Run the humanizer in embedded mode on the text between the markers; Jira renders single newlines as line breaks, so the output keeps one paragraph per line. Numbers and states must survive: 1.2.15, 3 of 3, 412 records, 0 failures, Wednesday.

---- TEXT STARTS ----

Great question! Here's a quick update on TICKET-42 🚀

To be clear, I'm not saying the outbox was wrong. Honestly? The root cause was elsewhere.

**Status:** The status is that the fix is in.

**Root Cause:** The record validation step was being skipped for messages whose payload had been re-serialized by the ERP connector -- a subtle but crucial issue that underscored the importance of end-to-end validation.

**Fix:** Validation is now enforced before publish. The previous approach of validating after publish has been removed. The new approach is more robust and represents a significant step forward.

**Verification:** 3 of 3 integration tests pass. 412 records were replayed against the dev event bus with 0 failures, highlighting the robustness of the new flow.

Release 1.2.15 is the vehicle for this change. It's not just a bugfix, it's a hardening of the whole pipeline. Deployment to the dev environment is planned for Wednesday, pending the usual cross-functional, well-known, end-to-end sign-off.

While specific details about the ACME-side retry policy are not fully documented, it likely retries three times.

Let me know if you have any questions. Hope this helps!

---- TEXT ENDS ----
