from __future__ import annotations

from core.config_store import config_store

from .auto_detection import detect_mail_import_content
from .schemas import (
    MailImportBatchDeleteRequest,
    MailImportDeleteItem,
    MailImportDetectionResponse,
    MailImportExecuteRequest,
    MailImportResponse,
    MailImportSummary,
)


class AutoMailImportService:
    def __init__(self, registry):
        self._registry = registry

    def detect(self, content: str) -> MailImportDetectionResponse:
        detection = detect_mail_import_content(content)
        return MailImportDetectionResponse(**detection.to_public_dict())

    def execute(self, request: MailImportExecuteRequest) -> MailImportResponse:
        detection = detect_mail_import_content(request.content)
        if not detection.rows:
            raise ValueError("邮箱导入内容为空")
        if not detection.can_import:
            first_message = next(
                (row.message for row in detection.rows if not row.resolved and row.message),
                "存在未识别的邮箱格式，请使用手动类型兜底",
            )
            raise ValueError(f"自动识别未通过：{first_message}")

        providers = [
            provider
            for provider in ("microsoft", "applemail")
            if detection.counts[provider] > 0
        ]
        is_mixed = len(providers) > 1
        responses: list[MailImportResponse] = []
        for provider in providers:
            delegated_request = request.model_copy(
                update={
                    "type": provider,
                    "content": detection.provider_content(provider),
                    "bind_to_config": request.bind_to_config and not is_mixed,
                }
            )
            try:
                responses.append(self._registry.get(provider).execute(delegated_request))
            except Exception:
                if provider == "applemail":
                    self._rollback_microsoft_responses(responses, request)
                raise

        if len(responses) == 1:
            return responses[0]
        if not responses:
            raise ValueError("未识别到可导入的邮箱记录")

        response_by_provider = {response.type: response for response in responses}
        applemail_response = response_by_provider.get("applemail")
        preferred_provider = (
            request.preferred_provider
            if request.preferred_provider in response_by_provider
            else None
        )
        config_payload: dict[str, str] = {}
        if applemail_response is not None:
            config_payload.update(
                {
                    "applemail_pool_dir": applemail_response.snapshot.pool_dir or request.pool_dir or "mail",
                    "applemail_pool_file": applemail_response.snapshot.filename,
                }
            )
        if preferred_provider is not None:
            config_payload["mail_provider"] = preferred_provider
        if request.bind_to_config and config_payload:
            config_store.set_many(config_payload)

        selected_response = (
            response_by_provider.get(preferred_provider)
            if preferred_provider is not None
            else None
        ) or responses[0]
        return MailImportResponse(
            type="auto",
            summary=MailImportSummary(
                total=sum(response.summary.total for response in responses),
                success=sum(response.summary.success for response in responses),
                failed=sum(response.summary.failed for response in responses),
            ),
            snapshot=selected_response.snapshot,
            errors=[error for response in responses for error in response.errors],
            meta={
                "providers": providers,
                "bound_to_config": bool(request.bind_to_config and preferred_provider),
                "bound_provider": preferred_provider,
                "applemail_pool_dir": (
                    applemail_response.snapshot.pool_dir
                    if applemail_response is not None
                    else ""
                ),
                "applemail_pool_file": (
                    applemail_response.snapshot.filename
                    if applemail_response is not None
                    else ""
                ),
                "snapshots": [response.snapshot.model_dump() for response in responses],
            },
        )

    def _rollback_microsoft_responses(
        self,
        responses: list[MailImportResponse],
        request: MailImportExecuteRequest,
    ) -> None:
        microsoft_response = next(
            (response for response in responses if response.type == "microsoft"),
            None,
        )
        if microsoft_response is None:
            return
        accounts = microsoft_response.meta.get("accounts")
        if not isinstance(accounts, list):
            return
        items = [
            MailImportDeleteItem(email=str(account.get("email") or "").strip())
            for account in accounts
            if isinstance(account, dict) and str(account.get("email") or "").strip()
        ]
        if not items:
            return
        rollback = self._registry.get("microsoft").batch_delete(
            MailImportBatchDeleteRequest(
                type="microsoft",
                items=items,
                preview_limit=request.preview_limit,
            )
        )
        if rollback.summary.failed:
            raise RuntimeError("混合导入失败，且已写入的微软邮箱未能完整回滚")
