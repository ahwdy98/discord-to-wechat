from typing import Any, Dict, Optional


def execute_cdp_command(driver: Any, command: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """Execute a Chrome DevTools Protocol command on local or remote WebDriver."""
    params = params or {}

    if hasattr(driver, "execute_cdp_cmd"):
        return driver.execute_cdp_cmd(command, params)

    executor = getattr(driver, "command_executor", None)
    commands = getattr(executor, "_commands", None)
    if isinstance(commands, dict) and "executeCdpCommand" not in commands:
        commands["executeCdpCommand"] = ("POST", "/session/$sessionId/goog/cdp/execute")

    result = driver.execute(
        "executeCdpCommand",
        {
            "cmd": command,
            "params": params,
        },
    )
    return result.get("value") if isinstance(result, dict) else result
