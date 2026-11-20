"""16:9 -> 9:16 face-tracked reframe for ClipForge.

Method (report §2 stage 5, artbyjazi/autoclip approach):

* Sample frames (default 2 fps) and run an OpenCV Haar-cascade face
  detector on each. The crop window (source height tall, 9:16 wide)
  centres on the largest detected face.
* The crop path is smoothed with a moving average — **except across a
  shot cut**: a frame-difference spike resets the smoothing window, so
  the crop never interpolates across a cut.
* Fallbacks, in order: no face in a frame -> hold the last good x;
  no cv2 installed -> centre crop for the whole clip; no faces at all
  -> centre crop. The module never fails the pipeline for lack of
  OpenCV.
* ``blurred_background=True`` requests the slides/screen-recording
  mode: instead of cropping away the sides, the full frame is kept
  blurred behind a centred 9:16 foreground. The render module reads
  this flag; this module reports the mode alongside the crop path.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

SAMPLE_FPS = 2.0
SMOOTH_WINDOW = 5  # frames in the moving average
SHOT_DIFF_THRESHOLD = 45.0  # mean absolute frame-diff (0-255) that = a cut


@dataclass
class CropPoint:
    """Crop-window left edge (x, in source pixels) at time ``t``."""

    t: float
    x: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReframePlan:
    """Result of planning a 9:16 reframe for one clip (or whole source)."""

    crop_points: list[CropPoint] = field(default_factory=list)
    crop_width: int = 0
    crop_height: int = 0
    source_width: int = 0
    source_height: int = 0
    blurred_background: bool = False
    face_tracked: bool = False
    shot_cuts_s: list[float] = field(default_factory=list)

    def crop_x_at(self, t: float) -> float:
        """Crop x for time ``t`` (nearest sampled point; centre if empty)."""
        if not self.crop_points:
            return max(0.0, (self.source_width - self.crop_width) / 2.0)
        nearest = min(self.crop_points, key=lambda p: abs(p.t - t))
        return nearest.x

    def to_dict(self) -> dict:
        d = asdict(self)
        d["crop_points"] = [p.to_dict() for p in self.crop_points]
        return d


def _centre_x(source_width: int, crop_width: int) -> float:
    return max(0.0, (source_width - crop_width) / 2.0)


def _smooth_with_cut_resets(
    xs: list[float | None], cuts: list[bool], window: int = SMOOTH_WINDOW
) -> list[float]:
    """Moving-average smoothing that restarts at every shot cut.

    ``xs`` holds a raw centre-x per sampled frame (None = no face);
    ``cuts[i]`` is True when frame i starts a new shot. A None holds
    the last good value; a cut clears the history so the average never
    blends positions from two different shots.
    """
    out: list[float] = []
    history: list[float] = []
    last = xs[0] if xs and xs[0] is not None else None
    for x, is_cut in zip(xs, cuts):
        if is_cut:
            history = []
        if x is not None:
            last = x
        if last is None:
            out.append(0.0)
            continue
        history.append(last)
        history = history[-window:]
        out.append(sum(history) / len(history))
    return out


def compute_crop_path(
    video_path: str,
    duration_s: float,
    source_width: int,
    source_height: int,
    start_s: float = 0.0,
    end_s: float | None = None,
    blurred_background: bool = False,
    sample_fps: float = SAMPLE_FPS,
) -> ReframePlan:
    """Plan the 9:16 crop for ``[start_s, end_s]`` of a video.

    Returns a :class:`ReframePlan`. With OpenCV available this is
    face-tracked with shot-boundary resets; without it (or with no
    faces) it is a centre crop — same shape, same contract.
    """
    end_s = duration_s if end_s is None else min(end_s, duration_s)
    crop_height = source_height
    crop_width = max(1, int(round(source_height * 9 / 16)))
    crop_width = min(crop_width, source_width)
    centre = _centre_x(source_width, crop_width)

    plan = ReframePlan(
        crop_width=crop_width,
        crop_height=crop_height,
        source_width=source_width,
        source_height=source_height,
        blurred_background=blurred_background,
    )
    if blurred_background:
        # Full frame is kept (blurred) behind the foreground; the crop
        # window is still reported (centred) for the foreground layer.
        plan.crop_points = [CropPoint(t=start_s, x=centre), CropPoint(t=end_s, x=centre)]
        return plan

    try:
        import cv2  # lazy: optional dependency
    except ImportError:
        plan.crop_points = [CropPoint(t=start_s, x=centre), CropPoint(t=end_s, x=centre)]
        return plan

    # Haar support varies by OpenCV build (OpenCV 5.x headless wheels
    # ship no CascadeClassifier / haarcascade data). Any failure here
    # falls back to a centre crop — the pipeline never fails for it.
    try:
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        face_cascade = cv2.CascadeClassifier(cascade_path)
        haar_ok = not face_cascade.empty()
    except (AttributeError, Exception):
        haar_ok = False
    if not haar_ok:
        plan.crop_points = [CropPoint(t=start_s, x=centre), CropPoint(t=end_s, x=centre)]
        return plan

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        plan.crop_points = [CropPoint(t=start_s, x=centre), CropPoint(t=end_s, x=centre)]
        return plan
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        step = max(1, int(round(fps / sample_fps)))
        raw_xs: list[float | None] = []
        cuts: list[bool] = []
        times: list[float] = []
        prev_gray = None
        frame_no = 0
        cap.set(cv2.CAP_PROP_POS_MSEC, start_s * 1000.0)
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            t = start_s + frame_no / fps
            if t > end_s:
                break
            if frame_no % step == 0:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                small = cv2.resize(gray, (0, 0), fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
                is_cut = False
                if prev_gray is not None:
                    prev_small = cv2.resize(
                        prev_gray, (small.shape[1], small.shape[0]), interpolation=cv2.INTER_AREA
                    )
                    diff = cv2.absdiff(small, prev_small)
                    if float(diff.mean()) > SHOT_DIFF_THRESHOLD:
                        is_cut = True
                        plan.shot_cuts_s.append(round(t, 3))
                prev_gray = gray
                faces = face_cascade.detectMultiScale(
                    small, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
                )
                face_x: float | None = None
                if len(faces):
                    # Largest face wins; coordinates scaled back to full res.
                    fx, _fy, fw, _fh = max(faces, key=lambda f: f[2] * f[3])
                    face_centre = (fx + fw / 2.0) * 2.0
                    face_x = face_centre - crop_width / 2.0
                    face_x = max(0.0, min(source_width - crop_width, face_x))
                    plan.face_tracked = True
                raw_xs.append(face_x)
                cuts.append(is_cut)
                times.append(round(t, 3))
            frame_no += 1
        if not raw_xs:
            plan.crop_points = [CropPoint(t=start_s, x=centre), CropPoint(t=end_s, x=centre)]
            return plan
        # Frames with no face start at centre; interior gaps hold the
        # last good value inside _smooth_with_cut_resets. If no face is
        # ever detected, the whole path is the centre crop.
        first_face = next((i for i, x in enumerate(raw_xs) if x is not None), None)
        if first_face is None:
            raw_xs = [centre] * len(raw_xs)
        else:
            for i in range(first_face):
                raw_xs[i] = centre
        smoothed = _smooth_with_cut_resets(raw_xs, cuts)
        plan.crop_points = [
            CropPoint(t=t, x=float(max(0.0, min(source_width - crop_width, x))))
            for t, x in zip(times, smoothed)
        ]
        return plan
    finally:
        cap.release()
