"""Switch-native collaboration, runtime, and memory integration.

The package intentionally keeps the Switch wire protocol in one place.  The
rest of the investigation code talks to :class:`SwitchRuntime`, never to HTTP
directly, so offline tests use an in-memory runtime with the same semantics.
"""

from ai_sre.switch.runtime import (
    BootstrapResult,
    FakeSwitchRuntime,
    HttpSwitchRuntime,
    SwitchRoom,
    SwitchRuntime,
    build_switch_runtime,
)

__all__ = [
    "BootstrapResult",
    "FakeSwitchRuntime",
    "HttpSwitchRuntime",
    "SwitchRoom",
    "SwitchRuntime",
    "build_switch_runtime",
]
