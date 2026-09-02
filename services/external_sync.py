"""外部系统同步（自动导入 / 回填）"""

from __future__ import annotations

import logging
from typing import Any

from services.chatgpt_sync import (
    _get_account_extra,
    persist_codex2api_sync_result,
    persist_cpa_sync_result,
    persist_sub2api_sync_result,
    upload_chatgpt_account_to_cpa,
)
from services.chatgpt_account_coordination import codex2api_account_mutation_lock


logger = logging.getLogger(__name__)


def _is_config_enabled(value: Any, default: bool = False) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    return normalized in {"1", "true", "yes", "on", "enabled"}


def _pick_text(source: Any, *keys: str, default: str = "") -> str:
    if not isinstance(source, dict):
        return default
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        text = value.strip() if isinstance(value, str) else str(value).strip()
        if text:
            return text
    return default


def _build_chatgpt_upload_account(account):
    class _A:
        pass

    upload_account = _A()
    upload_account.email = account.email
    extra = _get_account_extra(account)
    upload_account.access_token = (
        _pick_text(extra, "access_token", "accessToken") or account.token
    )
    upload_account.refresh_token = _pick_text(
        extra,
        "refresh_token",
        "refreshToken",
    )
    upload_account.id_token = _pick_text(extra, "id_token", "idToken")
    upload_account.session_token = _pick_text(
        extra,
        "session_token",
        "sessionToken",
    )
    upload_account.client_id = _pick_text(
        extra,
        "client_id",
        "clientId",
        default="app_EMoamEEZ73f0CkXaXp7hrann",
    )
    stored_user_id = str(getattr(account, "user_id", "") or "").strip()
    upload_account.workspace_id = _pick_text(
        extra,
        "workspace_id",
        "workspaceId",
        "chatgpt_account_id",
        "chatgptAccountId",
        default=stored_user_id,
    )
    upload_account.account_id = _pick_text(
        extra,
        "account_id",
        "accountId",
        "chatgpt_account_id",
        "chatgptAccountId",
        default=upload_account.workspace_id,
    )
    upload_account.user_id = _pick_text(
        extra,
        "chatgpt_user_id",
        "chatgptUserId",
        "user_id",
        "userId",
        default=stored_user_id,
    )
    return upload_account


def sync_codex2api_account(
    account,
    *,
    force: bool = False,
    replace_existing: bool = False,
    target: Any | None = None,
    client: Any | None = None,
) -> dict[str, Any] | None:
    """只同步 Codex2API，并独立记录该目标的结果。"""
    from core.config_store import config_store

    # New multi-target callers pass an explicit client.  Keeping this branch
    # separate preserves the long-tested legacy upload path and its exact
    # response semantics for existing login/relogin flows.
    if client is not None or target is not None:
        if client is None:
            from services.codex2api_target_client import Codex2APITargetClient, TargetConfig

            if isinstance(target, TargetConfig):
                client = Codex2APITargetClient(target)
            else:
                raise ValueError("explicit Codex2API target requires a TargetConfig or client")
        upload_account = _build_chatgpt_upload_account(account)
        payload: dict[str, Any] = {
            "name": _pick_text({"value": upload_account.email}, "value"),
            "email": _pick_text({"value": upload_account.email}, "value"),
        }
        for key in (
            "refresh_token",
            "access_token",
            "id_token",
            "session_token",
            "account_id",
            "workspace_id",
            "user_id",
            "client_id",
        ):
            value = _pick_text({"value": getattr(upload_account, key, "")}, "value")
            if value:
                payload[key] = value
        try:
            if payload.get("refresh_token") and payload.get("access_token"):
                response = client.import_full_json(payload)
            elif payload.get("refresh_token"):
                response = client.import_refresh_token(payload)
            elif payload.get("access_token"):
                response = client.import_access_token(payload)
            else:
                return {"name": "Codex2API", "ok": False, "msg": "账号缺少凭证"}
            response = response if isinstance(response, dict) else {}
            failed = int(response.get("failed") or 0)
            successful = sum(
                int(response.get(key) or 0)
                for key in ("success", "updated", "duplicate")
            )
            if failed or successful <= 0:
                message = str(
                    response.get("message")
                    or response.get("msg")
                    or response.get("error")
                    or "Codex2API 未确认账号已导入"
                ).strip()[:200]
                return {"name": "Codex2API", "ok": False, "msg": message}
            return {"name": "Codex2API", "ok": True, "msg": "目标账号已导入"}
        except Exception as exc:
            logger.error("Codex2API target sync failed (%s)", type(exc).__name__)
            return {"name": "Codex2API", "ok": False, "msg": "目标账号同步异常"}

    codex2api_enabled = _is_config_enabled(
        config_store.get("codex2api_enabled", "0"),
        default=False,
    )
    if not force and not codex2api_enabled:
        return None

    try:
        from platforms.chatgpt.codex2api_upload import upload_to_codex2api

        with codex2api_account_mutation_lock():
            ok, msg = upload_to_codex2api(
                _build_chatgpt_upload_account(account),
                replace_existing=replace_existing,
            )
    except Exception as exc:
        ok = False
        msg = "Codex2API 自动同步异常"
        logger.error("%s (%s)", msg, type(exc).__name__)

    try:
        persist_codex2api_sync_result(account, ok, msg)
    except Exception as exc:
        logger.error(
            "Codex2API sync state persistence failed (%s)",
            type(exc).__name__,
        )
        remote_message = str(msg or "").strip()
        msg = (
            f"{remote_message}，但同步状态保存失败"
            if remote_message
            else "Codex2API 同步状态保存失败"
        )
        ok = False
    return {"name": "Codex2API", "ok": ok, "msg": msg}


def sync_account(account) -> list[dict[str, Any]]:
    """根据平台将账号同步到外部系统。"""
    from core.config_store import config_store

    platform = getattr(account, "platform", "")
    results: list[dict[str, Any]] = []

    if platform == "chatgpt":
        upload_account = _build_chatgpt_upload_account(account)

        codex2api_result = sync_codex2api_account(account)
        if codex2api_result is not None:
            results.append(codex2api_result)

        # Codex2API 已按独立配置处理；贡献模式继续覆盖其余旧上传目标，避免重复上报。
        contribution_enabled = _is_config_enabled(config_store.get("contribution_enabled", "0"))
        if contribution_enabled:
            contribution_mode = str(config_store.get("contribution_mode", "codex") or "codex").strip().lower()

            if contribution_mode == "custom":
                # 自定义贡献系统模式
                custom_url = str(config_store.get("custom_contribution_url", "") or "").strip()
                custom_token = str(config_store.get("custom_contribution_token", "") or "").strip()
                if not custom_url:
                    msg = "自定义贡献服务器地址未配置"
                    persist_cpa_sync_result(account, False, msg)
                    results.append({"name": "CustomContribution", "ok": False, "msg": msg})
                    return results
                if not custom_token:
                    msg = "自定义贡献系统 token 未配置（请先绑定邮箱）"
                    persist_cpa_sync_result(account, False, msg)
                    results.append({"name": "CustomContribution", "ok": False, "msg": msg})
                    return results

                try:
                    import requests
                    from platforms.chatgpt.cpa_upload import generate_token_json

                    # 生成完整的 token JSON
                    extra = _get_account_extra(account)
                    token_json = generate_token_json(account)

                    # 如果 token_json 中没有 refresh_token，从 extra 获取
                    if not token_json.get("refresh_token"):
                        refresh_token = _pick_text(extra, "refresh_token", "refreshToken")
                        if refresh_token:
                            token_json["refresh_token"] = refresh_token
                    if not token_json.get("access_token"):
                        access_token = _pick_text(extra, "access_token", "accessToken") or getattr(account, "token", "")
                        if access_token:
                            token_json["access_token"] = access_token
                    if not token_json.get("id_token"):
                        id_token = _pick_text(extra, "id_token", "idToken")
                        if id_token:
                            token_json["id_token"] = id_token
                    if not token_json.get("client_id"):
                        client_id = _pick_text(extra, "client_id", "clientId")
                        if client_id:
                            token_json["client_id"] = client_id

                    refresh_token = str(token_json.get("refresh_token") or "").strip()
                    access_token = str(token_json.get("access_token") or "").strip()

                    # 验证必须有 refresh_token
                    if not refresh_token:
                        msg = "账号缺少 refresh_token"
                        persist_cpa_sync_result(account, False, msg)
                        results.append({"name": "CustomContribution", "ok": False, "msg": msg})
                        return results

                    resp = requests.post(
                        f"{custom_url.rstrip('/')}/api/upload",
                        json={
                            "email": account.email,
                            "refresh_token": refresh_token,
                            "access_token": access_token,
                            "token_json": token_json,
                        },
                        headers={"Authorization": f"Bearer {custom_token}"},
                        timeout=15,
                    )
                    data = resp.json()
                    if resp.status_code >= 400:
                        msg = data.get("error") or data.get("message") or str(data)
                        persist_cpa_sync_result(account, False, msg)
                        results.append({"name": "CustomContribution", "ok": False, "msg": msg})
                        return results

                    msg = f"上传成功: {data.get('message', '')}"
                    persist_cpa_sync_result(account, True, msg)
                    results.append({"name": "CustomContribution", "ok": True, "msg": msg})
                    return results
                except Exception as exc:
                    msg = f"上传到自定义贡献系统失败: {exc}"
                    persist_cpa_sync_result(account, False, msg)
                    results.append({"name": "CustomContribution", "ok": False, "msg": msg})
                    return results
            else:
                # codex2api 模式（原有逻辑）
                contribution_url = str(config_store.get("contribution_server_url", "") or "").strip()
                contribution_key = str(config_store.get("contribution_key", "") or "").strip()
                if not contribution_url:
                    msg = "Contribution 服务器地址未配置"
                    persist_cpa_sync_result(account, False, msg)
                    results.append({"name": "Contribution", "ok": False, "msg": msg})
                    return results

                ok, msg = upload_chatgpt_account_to_cpa(
                    account,
                    api_url=contribution_url,
                    api_key=contribution_key or None,
                )
                persist_cpa_sync_result(account, ok, msg)
                results.append({"name": "Contribution", "ok": ok, "msg": msg})
                return results

        cpa_url = str(config_store.get("cpa_api_url", "") or "").strip()
        cpa_enabled = _is_config_enabled(
            config_store.get("cpa_enabled", ""),
            default=bool(cpa_url),
        )
        if cpa_enabled and cpa_url:
            ok, msg = upload_chatgpt_account_to_cpa(account)
            persist_cpa_sync_result(account, ok, msg)
            results.append({"name": "CPA", "ok": ok, "msg": msg})

        codex_proxy_url = str(config_store.get("codex_proxy_url", "") or "").strip()
        if codex_proxy_url:
            upload_type = str(config_store.get("codex_proxy_upload_type", "at") or "at").strip().lower()
            extra = _get_account_extra(account)

            class _CP:
                pass

            cp = _CP()
            cp.access_token = _pick_text(extra, "access_token", "accessToken") or account.token
            cp.refresh_token = _pick_text(extra, "refresh_token", "refreshToken")

            if upload_type == "rt":
                from platforms.chatgpt.cpa_upload import upload_to_codex_proxy
                ok, msg = upload_to_codex_proxy(cp)
                results.append({"name": "CodexProxy(RT)", "ok": ok, "msg": msg})
            else:
                from platforms.chatgpt.cpa_upload import upload_at_to_codex_proxy
                ok, msg = upload_at_to_codex_proxy(cp)
                results.append({"name": "CodexProxy(AT)", "ok": ok, "msg": msg})

        # 关键逻辑：ChatGPT 现在支持同时回填 CPA 和 Sub2API，互不覆盖、分别上报结果。
        sub2api_url = str(config_store.get("sub2api_api_url", "") or "").strip()
        sub2api_key = str(config_store.get("sub2api_api_key", "") or "").strip()
        sub2api_enabled = _is_config_enabled(
            config_store.get("sub2api_enabled", ""),
            default=bool(sub2api_url and sub2api_key),
        )
        if sub2api_enabled and sub2api_url and sub2api_key:
            from platforms.chatgpt.sub2api_upload import upload_to_sub2api

            ok, msg = upload_to_sub2api(
                upload_account,
                api_url=sub2api_url,
                api_key=sub2api_key,
            )
            persist_sub2api_sync_result(account, ok, msg)
            results.append({"name": "Sub2API", "ok": ok, "msg": msg})

    elif platform == "grok":
        grok2api_url = str(config_store.get("grok2api_url", "") or "").strip()
        if grok2api_url:
            from services.grok2api_runtime import ensure_grok2api_ready
            from platforms.grok.grok2api_upload import upload_to_grok2api

            ready, ready_msg = ensure_grok2api_ready()
            if not ready:
                results.append({"name": "grok2api", "ok": False, "msg": ready_msg})
                return results

            ok, msg = upload_to_grok2api(account)
            results.append({"name": "grok2api", "ok": ok, "msg": msg})

    elif platform == "kiro":
        from platforms.kiro.account_manager_upload import resolve_manager_path, upload_to_kiro_manager

        configured_path = str(config_store.get("kiro_manager_path", "") or "").strip()
        target_path = resolve_manager_path(configured_path or None)
        if configured_path or target_path.parent.exists() or target_path.exists():
            ok, msg = upload_to_kiro_manager(account, path=configured_path or None)
            results.append({"name": "Kiro Manager", "ok": ok, "msg": msg})

    return results
