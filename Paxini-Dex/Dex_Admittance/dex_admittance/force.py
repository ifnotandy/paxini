from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class FilteredForce:
    sensor_vectors_n: tuple[tuple[float, float, float], ...]
    local_magnitudes_n: tuple[float, ...]
    index_n: float
    middle_n: float
    thumb_n: float
    palm_n: float
    index_local_max_n: float
    middle_local_max_n: float
    thumb_local_max_n: float
    palm_local_max_n: float


class ForceEstimator:
    """Per-sensor offsets and a first-order low-pass filter."""

    def __init__(
        self,
        index_indices: Sequence[int],
        middle_indices: Sequence[int],
        thumb_indices: Sequence[int],
        palm_indices: Sequence[int],
        cutoff_hz: float,
        baseline_vectors_n: Sequence[Sequence[float]] | None = None,
        index_weights: Sequence[float] | None = None,
        middle_weights: Sequence[float] | None = None,
        thumb_weights: Sequence[float] | None = None,
        palm_weights: Sequence[float] | None = None,
    ) -> None:
        if cutoff_hz <= 0:
            raise ValueError("cutoff_hz must be positive")
        self.index_indices = tuple(index_indices)
        self.middle_indices = tuple(middle_indices)
        self.thumb_indices = tuple(thumb_indices)
        self.palm_indices = tuple(palm_indices)
        self.index_weights = self._weights(index_weights, len(self.index_indices))
        self.middle_weights = self._weights(middle_weights, len(self.middle_indices))
        self.thumb_weights = self._weights(thumb_weights, len(self.thumb_indices))
        self.palm_weights = self._weights(palm_weights, len(self.palm_indices))
        self.cutoff_hz = float(cutoff_hz)
        if baseline_vectors_n is None:
            self.baseline = tuple((0.0, 0.0, 0.0) for _ in range(10))
        else:
            if len(baseline_vectors_n) != 10:
                raise ValueError("baseline must contain 10 sensor vectors")
            self.baseline = tuple(
                tuple(float(value) for value in vector)
                for vector in baseline_vectors_n
            )
        self._filtered: tuple[tuple[float, float, float], ...] | None = None
        self._last_timestamp: float | None = None

    def reset(self) -> None:
        self._filtered = None
        self._last_timestamp = None

    @property
    def last_timestamp(self) -> float | None:
        return self._last_timestamp

    def update(
        self,
        force_vectors_n: Sequence[Sequence[float]],
        timestamp_s: float,
    ) -> FilteredForce:
        if len(force_vectors_n) != 10:
            raise ValueError("force_vectors_n must contain 10 vectors")
        corrected = tuple(
            (
                float(vector[0]) - self.baseline[index][0],
                float(vector[1]) - self.baseline[index][1],
                max(0.0, float(vector[2]) - self.baseline[index][2]),
            )
            for index, vector in enumerate(force_vectors_n)
        )

        if self._filtered is None or self._last_timestamp is None:
            filtered = corrected
        else:
            dt = min(max(float(timestamp_s) - self._last_timestamp, 1e-4), 0.25)
            alpha = 1.0 - math.exp(-2.0 * math.pi * self.cutoff_hz * dt)
            filtered = tuple(
                tuple(
                    alpha * corrected[index][axis]
                    + (1.0 - alpha) * self._filtered[index][axis]
                    for axis in range(3)
                )
                for index in range(10)
            )
        self._filtered = filtered
        self._last_timestamp = float(timestamp_s)
        magnitudes = tuple(math.sqrt(sum(value * value for value in vector)) for vector in filtered)
        return FilteredForce(
            sensor_vectors_n=filtered,
            local_magnitudes_n=magnitudes,
            index_n=self._weighted_normal_sum(
                filtered, self.index_indices, self.index_weights
            ),
            middle_n=self._weighted_normal_sum(
                filtered, self.middle_indices, self.middle_weights
            ),
            thumb_n=self._weighted_normal_sum(
                filtered, self.thumb_indices, self.thumb_weights
            ),
            palm_n=self._weighted_normal_sum(
                filtered, self.palm_indices, self.palm_weights
            ),
            index_local_max_n=max(
                (magnitudes[index] for index in self.index_indices), default=0.0
            ),
            middle_local_max_n=max(
                (magnitudes[index] for index in self.middle_indices), default=0.0
            ),
            thumb_local_max_n=max(
                (magnitudes[index] for index in self.thumb_indices), default=0.0
            ),
            palm_local_max_n=max(
                (magnitudes[index] for index in self.palm_indices), default=0.0
            ),
        )

    @staticmethod
    def _weights(weights: Sequence[float] | None, count: int) -> tuple[float, ...]:
        if weights is None:
            return (1.0,) * count
        if len(weights) != count or any(weight <= 0 for weight in weights):
            raise ValueError("sensor weights must match channels and be positive")
        return tuple(float(weight) for weight in weights)

    @staticmethod
    def _weighted_normal_sum(
        vectors: Sequence[Sequence[float]],
        indices: Iterable[int],
        weights: Sequence[float],
    ) -> float:
        return sum(
            weight * max(0.0, float(vectors[index][2]))
            for index, weight in zip(indices, weights)
        )

    def raw_local_magnitudes(
        self, force_vectors_n: Sequence[Sequence[float]]
    ) -> tuple[float, ...]:
        if len(force_vectors_n) != 10:
            raise ValueError("force_vectors_n must contain 10 vectors")
        return tuple(
            math.sqrt(
                (float(vector[0]) - self.baseline[index][0]) ** 2
                + (float(vector[1]) - self.baseline[index][1]) ** 2
                + max(0.0, float(vector[2]) - self.baseline[index][2]) ** 2
            )
            for index, vector in enumerate(force_vectors_n)
        )
