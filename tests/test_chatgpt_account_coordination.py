import threading

from services.chatgpt_account_coordination import (
    chatgpt_account_operation_lock,
    codex2api_account_mutation_lock,
)


def test_same_account_lock_is_nonblocking_and_other_accounts_are_independent():
    with chatgpt_account_operation_lock(17, blocking=False) as first:
        assert first is True
        with chatgpt_account_operation_lock("17", blocking=False) as duplicate:
            assert duplicate is False
        with chatgpt_account_operation_lock(18, blocking=False) as other:
            assert other is True


def test_codex2api_mutation_lock_serializes_workers():
    entered = threading.Event()
    finished = threading.Event()

    def worker():
        with codex2api_account_mutation_lock():
            entered.set()
        finished.set()

    with codex2api_account_mutation_lock():
        thread = threading.Thread(target=worker)
        thread.start()
        assert entered.wait(0.05) is False
    assert finished.wait(1.0) is True
    thread.join(timeout=1.0)


def test_codex2api_mutation_lock_is_reentrant_in_one_thread():
    with codex2api_account_mutation_lock():
        with codex2api_account_mutation_lock():
            pass
