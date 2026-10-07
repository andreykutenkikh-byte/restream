from __future__ import annotations

import pytest

from app.broadcast.media_runtime import input_video_format


@pytest.mark.parametrize(
    "width,height,rate", [(1080, 1920, "30/1"), (1920, 1080, "60/1"), (1280, 720, "30000/1001")]
)
def test_detects_input_orientation_and_fractional_framerate(
    width: int, height: int, rate: str
) -> None:
    result = input_video_format(
        [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": width,
                "height": height,
                "r_frame_rate": rate,
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ]
    )
    assert result is not None and result[:2] == (width, height)
    numerator, denominator = rate.split("/")
    assert result[2] == pytest.approx(int(numerator) / int(denominator))


@pytest.mark.parametrize(
    "change",
    [
        {"codec_name": "hevc"},
        {"r_frame_rate": "0/0"},
        {"r_frame_rate": "NaN"},
        {"r_frame_rate": "120/1"},
        {"width": 7680},
    ],
)
def test_unsupported_input_remains_unqualified(change: dict[str, object]) -> None:
    video = {
        "codec_type": "video",
        "codec_name": "h264",
        "width": 1080,
        "height": 1920,
        "r_frame_rate": "30/1",
        **change,
    }
    assert input_video_format([video, {"codec_type": "audio", "codec_name": "aac"}]) is None
    assert input_video_format([video]) is None
