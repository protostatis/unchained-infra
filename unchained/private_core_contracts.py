"""Contracts for the public-to-private core boundary."""

from __future__ import annotations

CORE_EXECUTE_PATH = "/core/execute"

# Operation names accepted by the private core service.
OP_RUN_DDM = "run_ddm"
OP_RUN_INTEL = "run_intel"
OP_RUN_CDP_COMMAND = "run_cdp_command"
OP_RUN_JS = "run_js"
OP_NAVIGATE = "navigate"
OP_CLICK = "click"
OP_TYPE_TEXT = "type_text"
OP_PRESS_ENTER = "press_enter"
OP_SUBMIT_FORM = "submit_form"
OP_SCREENSHOT = "screenshot"
OP_CREATE_TAB = "create_tab"
OP_PROVISION_LAUNCH = "provision_launch"
OP_PROVISION_CLEANUP = "provision_cleanup"
OP_CLOSE_TAB = "close_tab"

PRIVATE_CORE_OPS = {
    OP_RUN_DDM,
    OP_RUN_INTEL,
    OP_RUN_CDP_COMMAND,
    OP_RUN_JS,
    OP_NAVIGATE,
    OP_CLICK,
    OP_TYPE_TEXT,
    OP_PRESS_ENTER,
    OP_SUBMIT_FORM,
    OP_SCREENSHOT,
    OP_CREATE_TAB,
    OP_PROVISION_LAUNCH,
    OP_PROVISION_CLEANUP,
    OP_CLOSE_TAB,
}

DEFAULT_PRIVATE_CORE_PORT = 8770
