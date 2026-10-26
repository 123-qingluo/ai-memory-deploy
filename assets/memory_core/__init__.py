"""ai-memory-deploy 核心包。

设计判断（不是算法）：把记忆当档案、把写入当审计、把读取当回忆、把遗忘当衰减。
"""
from .api import MemorySystem
from .models import Scope

__all__ = ["MemorySystem", "Scope"]
__version__ = "1.0.0"
