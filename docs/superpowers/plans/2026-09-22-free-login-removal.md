# Free subscription removal

User requested implementation and production deployment without another design review.

Confirmed Free subscriptions must terminate imported login, saved credential relogin,
and refresh-token maintenance before external upload. Imported mailboxes retain their
existing discard lifecycle. Saved accounts use identity-checked remote-first deletion,
including dependent auth cleanup. Deletion failure must remain visible and must never
fall through to upload. Unknown subscription results keep the account for retry.

- Force the existing subscription gate in `platforms/chatgpt/plugin.py` and
  `services/chatgpt_relogin.py`, including when persisted config disables it.
- Preserve the engine's `free_plan` result as a typed maintenance outcome; remove the
  saved account through `services/chatgpt_account_removal.py` before sync.
- Probe refreshed access tokens before sync. Persist rotated tokens so a temporary
  subscription lookup failure does not lose the replacement refresh token.
- Make import cleanup remove linked Codex2API credentials as well as the local row.
- Add regression tests covering Free, paid, unknown, deletion failures and task
  outcomes; run related authentication, task and removal suites.
- Review, commit and push; back up production, drain active tasks, deploy an immutable
  release, verify HTTP health and the new policy, then restore automatic maintenance.
