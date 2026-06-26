Task 8 README audit verification
Command: GIT_MASTER=1 git diff --check


Exit status: 0

Audit findings:
- Reviewed README control CLI/systemd scope, disabled forwarding, ledger storage, downstream retry, setup, and testing sections.
- README had to change because Task 1 added control_ledger.inbound_events with raw_body storage, while README still said the ledger does not store raw payload bodies.
- README now documents only implemented Task 1 behavior: inbound_events stores exact authenticated raw webhook request bytes and allowlisted Todoist headers.
- README now keeps older audit tables hash/metadata-only and does not claim token or secret exposure.
- README now avoids claiming disabled proxy webhook inbound/suppressed audit recording is complete. It says that work is still pending in restart-safe ACK.
- README still does not claim split ingress, external brokers, systemd/socket mutations, live Hermes config changes, prompt changes, replay UI, retry UI, or canary commands.
- Markdown LSP diagnostics were attempted for README.md but no .md LSP server is configured in this environment.

Files changed by Task 8:
- README.md
- .sisyphus/evidence/task-8-readme-audit.txt
- .sisyphus/evidence/task-8-disabled-docs.txt
- .sisyphus/notepads/restart-safe-ack/learnings.md

Final verification after evidence/notepad writes:
Command: GIT_MASTER=1 git diff --check


Final exit status: 0
