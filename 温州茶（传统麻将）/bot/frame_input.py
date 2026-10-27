"""One immutable PNG observation and its decoded pixels; never a frame cache."""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass(frozen=True, init=False, eq=False, slots=True)
class DecodedFrame:
    """Bind bytes to pixels decoded from those same bytes.

    Construction accepts only PNG bytes, never a separate image.  Callers must
    build a new object for every newly captured frame, including verification
    captures; an identical-looking image is not evidence that a shot is fresh.
    """

    raw: bytes = field(repr=False)
    image: np.ndarray = field(repr=False)

    def __init__(self, raw: bytes) -> None:
        if not isinstance(raw, bytes):
            raise TypeError("frame raw must be immutable bytes")
        if not raw:
            raise ValueError("截图不是有效 PNG 图像")
        decoded = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if decoded is None:
            raise ValueError("截图不是有效 PNG 图像")
        # Bytes-backed storage cannot be made writable by setflags(), unlike
        # setting WRITEABLE=False on an owning ndarray.  Recognition/annotation
        # consumers must copy explicitly if they need a mutable image.
        image = np.frombuffer(decoded.tobytes(), np.uint8).reshape(decoded.shape)
        object.__setattr__(self, "raw", raw)
        object.__setattr__(self, "image", image)

    @classmethod
    def from_png(cls, raw: bytes) -> DecodedFrame:
        return cls(raw)
