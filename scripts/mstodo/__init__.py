"""Microsoft To Do over the Microsoft Graph API — stdlib only.

Public surface:
    from mstodo.auth import DeviceCodeAuth
    from mstodo.graph import GraphClient
    from mstodo.todo import TodoService
"""

__version__ = "1.0.0"

USER_AGENT = f"ms-todo-skill/{__version__} (+https://github.com/byte-ish/ms-todo-skill)"

__all__ = ["USER_AGENT", "__version__"]
