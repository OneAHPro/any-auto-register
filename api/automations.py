from fastapi import APIRouter

from services.chatgpt_auto_relogin import get_chatgpt_auto_relogin_status


router = APIRouter(prefix="/automations", tags=["automations"])


@router.get("/chatgpt-relogin")
def get_chatgpt_relogin_automation_status():
    return get_chatgpt_auto_relogin_status()
