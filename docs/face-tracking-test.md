# Face-tracking test — synthetic talking-head video

Generated locally with `scripts/make_talking_head_video.py` (OpenCV-drawn
cartoon face: skin-tone ellipse, eyes, brows, talking mouth, shoulders —
no real person, no photo, no downloaded media) and run through the
existing face-tracked reframe (`clipforge.reframe.compute_crop_path`).
Run with `scripts/face_tracking_test.py`. This is a measurement, not a
pass/fail gate: the table states whether tracking engaged or the
centre-crop fallback ran.

| Video | OpenCV | Haar cascade | Mode | Crop points | Crop-x range | Centre x |
| --- | --- | --- | --- | --- | --- | --- |
| `talking_head_moving.mp4` | 4.10.0 | yes | **centre-crop fallback** | 20 | 0.0 px | 437.5 px |
| `talking_head_still.mp4` | 4.10.0 | yes | **centre-crop fallback** | 20 | 0.0 px | 437.5 px |
| `sample_60s.mp4` | 4.10.0 | yes | **centre-crop fallback** | 120 | 0.0 px | 437.5 px |

## How to read it

- **face-tracked**: the Haar cascade found a face in at least one sampled
  frame and the crop window followed it. A large crop-x range on the
  `moving` video (head swaying ±120 px) means the window actually moved
  with the speaker; a range near 0 on the `still` video is expected.
- **centre-crop fallback**: no face was detected (or no usable OpenCV),
  so the crop stayed centred. The pipeline still renders a valid
  1080x1920 clip — this is the designed fallback, not a failure.
- OpenCV 5.x headless wheels ship no `CascadeClassifier` / haarcascade
  data, so tracking cannot engage there even with cv2 installed; the
  reframe module detects that and falls back. OpenCV 4.10 headless does
  ship the cascade. The `vision` extra pins no major version, so
  deployments should pin `opencv-python-headless==4.10.*` until the
  reframe is moved to a DNN detector.

## Raw results

```json
[
  {
    "video": "/tmp/face-test/talking_head_moving.mp4",
    "width": 1280,
    "height": 720,
    "duration_s": 10.0,
    "opencv_version": "4.10.0",
    "haar_cascade_available": true,
    "face_tracked": false,
    "mode": "centre-crop fallback",
    "crop_points": 20,
    "crop_x_min": 437.5,
    "crop_x_max": 437.5,
    "crop_x_centre": 437.5,
    "crop_x_range": 0.0,
    "shot_cuts_s": []
  },
  {
    "video": "/tmp/face-test/talking_head_still.mp4",
    "width": 1280,
    "height": 720,
    "duration_s": 10.0,
    "opencv_version": "4.10.0",
    "haar_cascade_available": true,
    "face_tracked": false,
    "mode": "centre-crop fallback",
    "crop_points": 20,
    "crop_x_min": 437.5,
    "crop_x_max": 437.5,
    "crop_x_centre": 437.5,
    "crop_x_range": 0.0,
    "shot_cuts_s": []
  },
  {
    "video": "/home/hatch/workspace/clipforge/data/uploads/sample_60s.mp4",
    "width": 1280,
    "height": 720,
    "duration_s": 60.0,
    "opencv_version": "4.10.0",
    "haar_cascade_available": true,
    "face_tracked": false,
    "mode": "centre-crop fallback",
    "crop_points": 120,
    "crop_x_min": 437.5,
    "crop_x_max": 437.5,
    "crop_x_centre": 437.5,
    "crop_x_range": 0.0,
    "shot_cuts_s": []
  }
]
```

## What is still unverified

- Real-human face tracking (a real talking-head recording) — no such
  asset exists in this repo and none was downloaded for this test.
- Whisper/LLM live paths and platform uploads — still deferred, no keys.

## Follow-up probe (same videos, tuned detector settings)

To check whether the reframe's fixed detector settings were the
limiting factor, the first frame of the `moving` video was also run
through the cascade directly with `scaleFactor` 1.05/1.1/1.2,
`minNeighbors` 3/5, histogram equalisation on/off, and the `alt`,
`alt2` and `profileface` cascades. Result: **0 faces in every
combination**. The cartoon face does not trigger Haar at all — the
cascade is trained on real faces — so the synthetic fixture cannot
validate tracking engagement. It does validate the fallback contract:
a valid, centred 9:16 plan is always produced.
