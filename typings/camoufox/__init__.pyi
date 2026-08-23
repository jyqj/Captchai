"""Minimal stub for the optional camoufox runtime dependency.

Camoufox is only installed when BROWSER_ENGINE=camoufox is used; these stubs
let pyright check src/services/browser.py without the package present.
"""

from typing import Any

async def AsyncNewBrowser(*args: Any, **kwargs: Any) -> Any: ...
