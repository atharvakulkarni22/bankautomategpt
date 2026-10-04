"""Human takeover and resume: what happens when the automation cannot continue alone.

Both bag.replay and bag.agent use this package, so it must not import either of them. It also
never imports Playwright: it works through the Surface interface only.
"""

from .human_recorder import HUMAN_RECORDER_JS, HumanRecorder, summarize
from .resume import (
    ABORT_WORDS,
    DEFAULT_DIR,
    LineReader,
    find_intervention,
    flag_path,
    list_interventions,
    make_enter_checker,
    request_resume,
)
from .states import ControlController, ControlState, HandoffError, InvalidTransition, Transition
from .takeover import HumanTakeover, Intervention, TakeoverResult

__all__ = [
    "ABORT_WORDS",
    "ControlController",
    "ControlState",
    "DEFAULT_DIR",
    "HUMAN_RECORDER_JS",
    "HandoffError",
    "HumanRecorder",
    "HumanTakeover",
    "Intervention",
    "InvalidTransition",
    "LineReader",
    "TakeoverResult",
    "Transition",
    "find_intervention",
    "flag_path",
    "list_interventions",
    "make_enter_checker",
    "request_resume",
    "summarize",
]
