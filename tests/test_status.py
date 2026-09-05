"""运行状态机测试：验证合法路径可走、非法跳转被拒绝。"""
import pytest

from warden_agent.core.run.status import AgentRun, IllegalStateTransition, RunStatus


def test_正常完成_全链路() -> None:
    run = AgentRun("run-1")
    run.mark_queued()
    run.start()
    run.begin_completing()
    run.complete()
    assert run.status == RunStatus.COMPLETED
    assert run.is_terminal()


def test_暂停与恢复() -> None:
    run = AgentRun("run-2")
    run.mark_queued()
    run.start()
    run.request_suspend()
    run.suspend()
    assert run.status == RunStatus.SUSPENDED
    run.resume()
    assert run.status == RunStatus.RUNNING


def test_等待审批_再回到运行() -> None:
    run = AgentRun("run-3")
    run.mark_queued()
    run.start()
    run.wait_for_approval()
    assert run.status == RunStatus.WAITING_APPROVAL
    run.resume()  # 审批通过后回到运行
    assert run.status == RunStatus.RUNNING


def test_任意跳转被拒绝() -> None:
    """直接从 PENDING 跳到 SUSPENDED 是不允许的，必须走 SUSPENDING。"""
    run = AgentRun("run-4")
    with pytest.raises(IllegalStateTransition):
        run.suspend()


def test_收尾前直接完成被拒绝() -> None:
    """complete 必须先经过 begin_completing，不能从 RUNNING 直接完成。"""
    run = AgentRun("run-5")
    run.mark_queued()
    run.start()
    with pytest.raises(IllegalStateTransition):
        run.complete()


def test_终态不可再变() -> None:
    run = AgentRun("run-6")
    run.mark_queued()
    run.start()
    run.begin_completing()
    run.complete()
    assert run.is_terminal()
    with pytest.raises(IllegalStateTransition):
        run.cancel()  # 终态不能再进入任何状态


def test_失败可来自任意非终态() -> None:
    run = AgentRun("run-7")
    run.mark_queued()
    run.fail()
    assert run.status == RunStatus.FAILED
