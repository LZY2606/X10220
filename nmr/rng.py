"""固定 seed 的随机源：保证演示、打分平局与测试完全可复现。"""
from __future__ import annotations

import numpy as np


def seeded_rng(seed: int = 20260922) -> np.random.Generator:
    return np.random.default_rng(seed)
