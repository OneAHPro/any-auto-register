from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session

from core.db import AccountModel, engine
from platforms.chatgpt.status_probe import probe_local_chatgpt_status
from services.chatgpt_account_state import apply_chatgpt_status_policy


def _to_probe_account(account: AccountModel):
    extra = account.get_extra()

    class ProbeAccount:
        pass

    probe_account = ProbeAccount()
    probe_account.email = account.email
    probe_account.user_id = account.user_id
    probe_account.token = account.token
    probe_account.extra = extra
    probe_account.access_token = extra.get("access_token") or account.token
    probe_account.refresh_token = extra.get("refresh_token", "")
    probe_account.id_token = extra.get("id_token", "")
    probe_account.session_token = extra.get("session_token", "")
    probe_account.client_id = extra.get(
        "client_id",
        "app_EMoamEEZ73f0CkXaXp7hrann",
    )
    probe_account.cookies = extra.get("cookies", "")
    return probe_account


def refresh_chatgpt_account_by_id(account_id: int) -> dict:
    with Session(engine) as session:
        account = session.get(AccountModel, int(account_id))
        if not account or account.platform != "chatgpt":
            raise RuntimeError("ChatGPT 账号不存在")

        extra = account.get_extra()
        proxy = str(extra.get("proxy_used") or "").strip() or None
        probe = probe_local_chatgpt_status(_to_probe_account(account), proxy=proxy)
        extra["chatgpt_local"] = probe
        account.set_extra(extra)
        apply_chatgpt_status_policy(account, local_probe=probe)
        account.updated_at = datetime.now(timezone.utc)
        session.add(account)
        session.commit()
        return probe
