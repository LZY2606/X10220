import numpy as np
import pytest

from nmrwb.core import Store


@pytest.fixture()
def vclock():
    """虚拟时间：固定、单调、不依赖开发者机器时钟。"""
    t = {"n": 0}

    def clock():
        t["n"] += 1
        n = t["n"]
        return "2026-01-01T00:%02d:%02dZ" % (n // 60, n % 60)

    return clock


@pytest.fixture()
def store(tmp_path, vclock):
    np.random.seed(0)  # 固定 seed（引擎本身确定性，此处为显式约束）
    return Store(str(tmp_path / "test.db"), clock=vclock)
