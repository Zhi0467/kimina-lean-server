import logging
import sys
from typing import Any

from .async_client import AsyncKiminaClient, ExecRequestOverloadedError
from .exec_env import AsyncLeanExecBatcher, AsyncLeanExecEnv
from .exec_journal import (
    ExecMicrobatchJournal,
    ExecMicrobatchRecord,
    UncertainMicrobatchError,
)
from .exec_server import ExecServerConfig, launch_server
from .exec_models import (
    ExecCancelRequest,
    ExecCancelResponse,
    ExecCancelResult,
    ExecCleanupRequest,
    ExecCleanupResponse,
    ExecCleanupResult,
    ExecDebugInfo,
    ExecDiagnostics,
    ExecLimitsResponse,
    ExecMessage,
    ExecStatsResponse,
    ExecPos,
    ExecWorkerPoolStats,
    ExecWorkerStats,
    ExecStateStoreStats,
    ExecLifecycleStats,
    ExecRequestLimiterStats,
    ExecObservedMetrics,
    ExecCreateStateItem,
    ExecCreateStatesRequest,
    ExecCreateStatesResponse,
    ExecCreateStatesResult,
    ExecGoalInfo,
    ExecHypothesis,
    ExecStateInfo,
    ExecStatus,
    ExecStepBatchItem,
    ExecStepBatchRequest,
    ExecStepBatchResponse,
    ExecStepBatchResult,
    ExecStepResult,
    ExecVerifyItem,
    ExecVerifyRequest,
    ExecVerifyResponse,
    ExecVerifyResult,
    ExecVerifyStatus,
)
from .models import (
    BackwardResponse,
    CheckRequest,
    CheckResponse,
    Code,
    Command,
    CommandResponse,
    Diagnostics,
    Error,
    ExtendedCommandResponse,
    ExtendedError,
    Infotree,
    Message,
    ReplRequest,
    ReplResponse,
    Snippet,
    SnippetAnalysis,
    SnippetStatus,
    VerifyRequestBody,
    VerifyResponse,
)
from .sync_client import KiminaClient

__all__ = [
    "AsyncKiminaClient",
    "AsyncLeanExecBatcher",
    "AsyncLeanExecEnv",
    "BackwardResponse",
    "ReplRequest",
    "ReplResponse",
    "CheckRequest",
    "CheckResponse",
    "Code",
    "Command",
    "CommandResponse",
    "Diagnostics",
    "Error",
    "ExtendedCommandResponse",
    "ExtendedError",
    "ExecCleanupRequest",
    "ExecCleanupResponse",
    "ExecCleanupResult",
    "ExecCancelRequest",
    "ExecCancelResponse",
    "ExecCancelResult",
    "ExecCreateStateItem",
    "ExecCreateStatesRequest",
    "ExecCreateStatesResponse",
    "ExecCreateStatesResult",
    "ExecDebugInfo",
    "ExecDiagnostics",
    "ExecLimitsResponse",
    "ExecMessage",
    "ExecPos",
    "ExecStatsResponse",
    "ExecWorkerPoolStats",
    "ExecWorkerStats",
    "ExecStateStoreStats",
    "ExecLifecycleStats",
    "ExecRequestLimiterStats",
    "ExecObservedMetrics",
    "ExecRequestOverloadedError",
    "ExecServerConfig",
    "ExecMicrobatchJournal",
    "ExecMicrobatchRecord",
    "ExecGoalInfo",
    "ExecHypothesis",
    "ExecStateInfo",
    "ExecStatus",
    "ExecStepBatchItem",
    "ExecStepBatchRequest",
    "ExecStepBatchResponse",
    "ExecStepBatchResult",
    "ExecStepResult",
    "ExecVerifyItem",
    "ExecVerifyRequest",
    "ExecVerifyResponse",
    "ExecVerifyResult",
    "ExecVerifyStatus",
    "Infotree",
    "KiminaClient",
    "Message",
    "Snippet",
    "SnippetAnalysis",
    "SnippetStatus",
    "UncertainMicrobatchError",
    "VerifyRequestBody",
    "VerifyResponse",
    "launch_server",
]

from colorama import Fore, Style, init

init(autoreset=True)


class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: Fore.CYAN,
        logging.INFO: Fore.WHITE,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Fore.MAGENTA + Style.BRIGHT,
    }

    def format(self, record: Any) -> str:
        log_color = self.COLORS.get(record.levelno, "")
        message = super().format(record)
        return f"{log_color}{message}{Style.RESET_ALL}"


logger = logging.getLogger("lean-client")

if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = ColorFormatter("%(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
