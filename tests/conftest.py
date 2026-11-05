from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from csvagent.data import Dataset
from csvagent.tools import Toolbox


def synthetic_heart(n: int = 300, seed: int = 0) -> pd.DataFrame:
    """Small SYNTHETIC dataset shaped like a heart disease table (not real patients)."""
    rng = np.random.default_rng(seed)
    age = rng.integers(30, 78, n)
    sex = rng.choice(["M", "F"], n, p=[0.7, 0.3])
    cp = rng.choice(["ASY", "NAP", "ATA", "TA"], n)
    chol = rng.normal(230, 45, n).round()
    chol[rng.random(n) < 0.1] = 0  # disguised missing values, like the real dataset
    maxhr = (205 - 0.8 * age + rng.normal(0, 15, n)).round()
    logit = -6 + 0.08 * age + 1.0 * (sex == "M") + 1.4 * (cp == "ASY") - 0.01 * maxhr
    target = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(int)
    return pd.DataFrame(
        {
            "Age": age,
            "Sex": sex,
            "ChestPainType": cp,
            "Cholesterol": chol,
            "MaxHR": maxhr,
            "HeartDisease": target,
        }
    )


@pytest.fixture
def data():
    return Dataset(synthetic_heart(), "heart_synthetic")


@pytest.fixture
def toolbox(data, tmp_path):
    return Toolbox(data, tmp_path / "out")


# ----------------------------------------------------------------- fake Anthropic client


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(id_, name, **inp):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=inp)


def response(*blocks, stop="tool_use"):
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop,
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )


class FakeClient:
    """Replays scripted responses and records every request."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        # deep-ish copy of messages so later appends don't change what we recorded
        self.requests.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        return self._responses.pop(0)


@pytest.fixture
def fake():
    return SimpleNamespace(Client=FakeClient, text=text, tool_use=tool_use, response=response)
