"""YUV frame presentation for the AirFrame desktop window (plan stage 6).

Two presentation back ends sit behind one small interface:

* ``QtVideoRenderer`` - the minimal Qt Multimedia path: the PyAV YUV planes are
  copied into a ``QVideoFrame`` with the stream's colour metadata and handed to
  the ``QVideoSink`` of a ``QVideoWidget``. On a real window Qt renders the
  frame through QRhi (Metal on macOS) and its ``yuv_triplanar`` shader, so the
  mandatory per-frame ``reformat(format="bgra")`` CPU conversion is gone from
  the main path. The copy of the YUV planes into Qt's frame stays (Qt does not
  expose a writable external-buffer frame through PySide6), so this is *not*
  zero-copy; it is one 1.5 bytes-per-pixel copy instead of a full BGRA swscale
  conversion. Qt downloads/uploading the frame is Qt's business - the evidence
  script records the RHI backend that actually drew it.
* ``SoftwareVideoRenderer`` - the honest fallback used when no GPU/RHI path is
  available. It keeps the exact old conversion (PyAV/swscale to BGRA + QImage
  painted by QPainter), so it can never look different from the previous app.

Colour handling: PyAV frame metadata (BT.601/BT.709, limited/full range) is
mapped onto ``QVideoFrameFormat``. The pinned Qt 6.12.0 RHI conversion was
measured against the ITU reference tables in a real window (see
scripts/verify-render-native.py): BT.601 limited, BT.709 limited and BT.709
full match within 2 code values, while BT.601 full drifts by up to 26 code
values. The verified set is ``GPU_SUPPORTED_COLOURS``; unverified combinations
are routed through the software conversion by the same renderer, so the video
stays correct and the reason is visible in the diagnostics.

A frame handed to a renderer is pinned until it was converted/consumed; a
mailbox replacement, window close or session switch never frees a frame that
is still in use.
"""
import math
import threading

from PySide6.QtCore import QObject, QRect, QSize, Qt, Signal, Slot
from PySide6.QtGui import QColor, QImage, QGuiApplication, QPainter, QTransform
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat, QtVideo
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QStackedLayout, QWidget

MATRIX_BT601 = "bt601"
MATRIX_BT709 = "bt709"
RANGE_LIMITED = "limited"
RANGE_FULL = "full"
# Defaults reproduce the old swscale (PyAV reformat) behaviour for streams whose
# SPS carries no colour description: BT.601 limited. This keeps the new path
# colour-identical to the previous app for metadata-less Android streams.
DEFAULT_MATRIX = MATRIX_BT601
DEFAULT_RANGE = RANGE_LIMITED
# Verified by real-window screen capture against the ITU reference tables
# (artifacts/2026-10-30-stable60fps/native/). BT.601+full is excluded: the
# pinned Qt 6.12.0 RHI conversion measured up to 26 code values off there.
GPU_SUPPORTED_COLOURS = frozenset(
    {(MATRIX_BT601, RANGE_LIMITED), (MATRIX_BT709, RANGE_LIMITED), (MATRIX_BT709, RANGE_FULL)}
)

_PIXEL_FORMATS = {
    "yuv420p": (QVideoFrameFormat.PixelFormat.Format_YUV420P, 3),
    "nv12": (QVideoFrameFormat.PixelFormat.Format_NV12, 2),
}
_COLOURSPACE_ITU709 = 1  # av.video.reformatter.Colorspace.ITU709
_COLOR_RANGE_JPEG = 2  # av.video.reformatter.ColorRange.JPEG
_ROTATIONS = {
    0: QtVideo.Rotation.None_,
    90: QtVideo.Rotation.Clockwise90,
    180: QtVideo.Rotation.Clockwise180,
    270: QtVideo.Rotation.Clockwise270,
}


class UnsupportedPixelFormat(Exception):
    """The GPU frame bridge cannot represent this PyAV pixel format."""


def ccw_to_cw(degrees):
    """PyAV reports display-matrix rotation counter-clockwise; Qt wants clockwise."""
    return int((-int(degrees)) % 360)


def display_rotation_from_frame(frame):
    """Clockwise display rotation in degrees for a PyAV frame (0 when absent)."""
    return ccw_to_cw(getattr(frame, "rotation", 0) or 0)



class ColourSpec:
    """Matrix + range description of a decoded frame."""

    __slots__ = ("matrix", "range")

    def __init__(self, matrix=DEFAULT_MATRIX, range=DEFAULT_RANGE):
        if matrix not in (MATRIX_BT601, MATRIX_BT709):
            raise ValueError(f"unsupported colour matrix: {matrix!r}")
        if range not in (RANGE_LIMITED, RANGE_FULL):
            raise ValueError(f"unsupported colour range: {range!r}")
        self.matrix = matrix
        self.range = range

    @classmethod
    def from_av_frame(cls, frame):
        colorspace = int(getattr(frame, "colorspace", 0) or 0)
        matrix = MATRIX_BT709 if colorspace == _COLOURSPACE_ITU709 else DEFAULT_MATRIX
        color_range = int(getattr(frame, "color_range", 0) or 0)
        full = color_range == _COLOR_RANGE_JPEG
        return cls(matrix, RANGE_FULL if full else RANGE_LIMITED)

    @property
    def gpu_supported(self):
        return (self.matrix, self.range) in GPU_SUPPORTED_COLOURS

    def label(self):
        return f"{self.matrix}/{self.range}"

    def __eq__(self, other):
        return isinstance(other, ColourSpec) and (self.matrix, self.range) == (other.matrix, other.range)

    def __hash__(self):
        return hash((self.matrix, self.range))

    def __repr__(self):
        return f"ColourSpec({self.matrix!r}, {self.range!r})"


def _qt_rotation(degrees):
    rotation = _ROTATIONS.get(int(degrees) % 360)
    if rotation is None:
        raise ValueError(f"unsupported rotation: {degrees!r}")
    return rotation


def _plane_data_bytes(source, index):
    """Bytes per plane row that carry samples (PyAV reports NV12 chroma as U/V pairs)."""
    plane = source.planes[index]
    if source.format.name == "nv12" and index == 1:
        return 2 * plane.width
    return plane.width


def _copy_planes(source, target):
    """Copy each PyAV plane into the Qt-allocated YUV plane, honouring both strides."""
    for index in range(target.planeCount()):
        src = source.planes[index]
        dst = target.bits(index)
        dst_stride = target.bytesPerLine(index)
        dst_rows = target.mappedBytes(index) // max(1, dst_stride)
        rows = min(src.height, dst_rows)
        width = min(_plane_data_bytes(source, index), dst_stride)
        src_mv = memoryview(src)
        if rows == src.height and dst_rows == rows and src.line_size == dst_stride == width:
            # Contiguous plane: one copy instead of per-row Python slicing.
            dst[0:rows * dst_stride] = src_mv[0:rows * src.line_size]
            continue
        for row in range(rows):
            start = row * src.line_size
            offset = row * dst_stride
            dst[offset:offset + width] = src_mv[start:start + width]


def qvideo_frame_from_av(frame, rotation=None):
    """Build a Qt YUV frame from PyAV planes without any RGB conversion.

    Raises ``UnsupportedPixelFormat`` for pixel formats the Qt bridge cannot
    express; callers then use the software conversion instead of guessing.
    """
    pixel_format = _PIXEL_FORMATS.get(frame.format.name)
    if pixel_format is None:
        raise UnsupportedPixelFormat(frame.format.name)
    qt_pixel_format, plane_count = pixel_format
    if len(frame.planes) != plane_count:
        raise UnsupportedPixelFormat(f"{frame.format.name}: {len(frame.planes)} planes")
    if rotation is None:
        rotation = display_rotation_from_frame(frame)
    rotation = int(rotation) % 360
    spec = ColourSpec.from_av_frame(frame)
    fmt = QVideoFrameFormat(QSize(frame.width, frame.height), qt_pixel_format)
    fmt.setColorSpace(
        QVideoFrameFormat.ColorSpace.ColorSpace_BT709
        if spec.matrix == MATRIX_BT709
        else QVideoFrameFormat.ColorSpace.ColorSpace_BT601
    )
    fmt.setColorRange(
        QVideoFrameFormat.ColorRange.ColorRange_Full
        if spec.range == RANGE_FULL
        else QVideoFrameFormat.ColorRange.ColorRange_Video
    )
    qt_rotation = _qt_rotation(rotation) if rotation else None
    if qt_rotation is not None:
        fmt.setRotation(qt_rotation)
    qframe = QVideoFrame(fmt)
    if qt_rotation is not None:
        qframe.setRotation(qt_rotation)
    if not qframe.map(QVideoFrame.MapMode.WriteOnly):
        raise UnsupportedPixelFormat(f"{frame.format.name}: Qt frame is not writable")
    try:
        _copy_planes(frame, qframe)
    finally:
        qframe.unmap()
    return qframe


def software_image_from_av(frame, rotation=None):
    """Old software conversion kept as the honest fallback: swscale YUV -> BGRA -> QImage."""
    if rotation is None:
        rotation = display_rotation_from_frame(frame)
    rotation = int(rotation) % 360
    converted = frame.reformat(format="bgra")
    plane = converted.planes[0]
    image = QImage(
        memoryview(plane), converted.width, converted.height, plane.line_size, QImage.Format.Format_ARGB32
    ).copy()  # detach from the PyAV buffer: QImage must own its pixels
    if rotation:
        image = image.transformed(QTransform().rotate(rotation))
    return image


def fitted_rect(frame_size, viewport, rotation=0, exact=False):
    """Target rect for a frame in a viewport, preserving aspect ratio (letterbox)."""
    width, height = int(frame_size.width()), int(frame_size.height())
    if int(rotation) % 180:
        width, height = height, width
    view_width, view_height = int(viewport.width()), int(viewport.height())
    if exact:
        return QRect(0, 0, max(0, view_width), max(0, view_height))
    if width <= 0 or height <= 0 or view_width <= 0 or view_height <= 0:
        return QRect(0, 0, 0, 0)
    scale = min(view_width / width, view_height / height)
    target_width = max(1, int(math.floor(width * scale + 0.5)))
    target_height = max(1, int(math.floor(height * scale + 0.5)))
    return QRect((view_width - target_width) // 2, (view_height - target_height) // 2, target_width, target_height)


def exact_window_size(frame_physical, dpr):
    """Logical window size whose device pixels are *at least* the requested physical frame."""
    dpr = float(dpr) if dpr and float(dpr) > 0 else 1.0
    return QSize(
        int(math.ceil(int(frame_physical.width()) / dpr)),
        int(math.ceil(int(frame_physical.height()) / dpr)),
    )


def physical_size(logical, dpr):
    """Device pixels covered by a logical size (rounded up: never downscale)."""
    dpr = float(dpr) if dpr and float(dpr) > 0 else 1.0
    return QSize(
        int(math.ceil(int(logical.width()) * dpr)),
        int(math.ceil(int(logical.height()) * dpr)),
    )


class PreparedFrame:
    """A frame converted for one consumer; pins its source until presented."""

    __slots__ = ("serial", "colour", "width", "height", "rotation", "qframe", "image", "source")

    def __init__(self, serial, colour, width, height, rotation, qframe=None, image=None, source=None):
        self.serial = serial
        self.colour = colour
        self.width = width
        self.height = height
        self.rotation = rotation
        self.qframe = qframe
        self.image = image
        self.source = source

    def output_size(self):
        if self.rotation % 180:
            return QSize(self.height, self.width)
        return QSize(self.width, self.height)

    def __repr__(self):
        return (
            f"PreparedFrame(serial={self.serial}, {self.width}x{self.height}, "
            f"colour={self.colour.label()}, rotation={self.rotation})"
        )


class _SoftwareSurface(QWidget):
    """CPU-painted surface used by the fallback renderer and by unverified frames."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image = None
        self.setMinimumSize(1, 1)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

    def set_image(self, image):
        self._image = image
        self.update()

    def image(self):
        return self._image

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        if self._image is not None and not self._image.isNull():
            target = fitted_rect(self._image.size(), self.size())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(target, self._image)
        painter.end()


class Renderer:
    """Small interface: prepare off the GUI thread, present on the GUI thread."""

    requested_mode = "software"
    mode = "software"

    def __init__(self):
        self.fallback_reason = None
        self._widget = None
        self._closed = False
        self._latest = None
        self._prepared_frames = 0
        self._presented_frames = 0
        self._skipped_hidden = 0
        self._dropped_closed = 0
        self._software_route = 0
        self._gpu_route = 0

    def widget(self):
        return self._widget

    def wants_frame(self):
        """False when a hidden/closed window must not spend conversion work."""
        return not self._closed and self._widget is not None and self._widget.isVisible()

    def prepare(self, frame, *, serial):
        raise NotImplementedError

    def _render(self, prepared):
        raise NotImplementedError

    def present(self, prepared):
        if self._closed:
            self._dropped_closed += 1
            return False
        self._latest = prepared
        if not self._widget.isVisible():
            self._skipped_hidden += 1
            return False
        self._render(prepared)
        self._presented_frames += 1
        return True

    def refresh(self):
        """Present the latest prepared frame after becoming visible (no extra work)."""
        if self._closed or self._latest is None or not self._widget.isVisible():
            return False
        self._render(self._latest)
        self._presented_frames += 1
        return True

    def clear(self):
        self._latest = None

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._latest = None

    def output_path(self):
        return self.mode

    def rhi_backend(self):
        return None

    def stats(self):
        return {
            "mode": self.mode,
            "requestedMode": self.requested_mode,
            "prepared": self._prepared_frames,
            "framesPrepared": self._prepared_frames,
            "framesPresented": self._presented_frames,
            "framesSkippedHidden": self._skipped_hidden,
            "framesDroppedClosed": self._dropped_closed,
            "framesGpuRoute": self._gpu_route,
            "framesSoftwareRoute": self._software_route,
            "outputPath": self.output_path(),
            "fallbackReason": self.fallback_reason,
        }


class QtVideoRenderer(Renderer):
    """GPU candidate: QVideoFrame (YUV) -> QVideoWidget's QVideoSink (QRhi shader)."""

    requested_mode = "gpu"
    mode = "gpu"

    def __init__(self):
        super().__init__()
        self._container = QWidget()
        self._layout = QStackedLayout(self._container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._video = QVideoWidget()
        self._video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self._surface = _SoftwareSurface()
        self._layout.addWidget(self._video)
        self._layout.addWidget(self._surface)
        self._widget = self._container
        self._sink = self._video.videoSink()
        self._route = "gpu"

    def video_sink(self):
        return self._sink

    def prepare(self, frame, *, serial):
        colour = ColourSpec.from_av_frame(frame)
        rotation = display_rotation_from_frame(frame)
        qframe = None
        if colour.gpu_supported and frame.format.name in _PIXEL_FORMATS:
            try:
                qframe = qvideo_frame_from_av(frame, rotation=rotation)
            except UnsupportedPixelFormat:
                qframe = None
        prepared = PreparedFrame(
            serial=serial,
            colour=colour,
            width=frame.width,
            height=frame.height,
            rotation=rotation,
            qframe=qframe,
            source=frame,  # pinned until present() converted/uploaded it
        )
        self._prepared_frames += 1
        return prepared

    def _render(self, prepared):
        if prepared.qframe is not None:
            self._layout.setCurrentWidget(self._video)
            self._sink.setVideoFrame(prepared.qframe)
            self._route = "gpu"
            self._gpu_route += 1
            self.fallback_reason = None
            return
        # Frame/colour combination not verified through the RHI shader: keep the
        # video displayed through the software conversion and say why.
        image = prepared.image
        if image is None:
            image = software_image_from_av(prepared.source, rotation=prepared.rotation)
        self._surface.set_image(image)
        self._layout.setCurrentWidget(self._surface)
        self._route = "software"
        self._software_route += 1
        self.fallback_reason = (
            f"software colour conversion: {prepared.colour.label()} is not verified on the GPU path"
        )
        prepared.image = image

    def rhi_backend(self):
        try:
            rhi = self._sink.rhi()
        except Exception:  # pragma: no cover - defensive across Qt patch versions
            return None
        if rhi is None:
            return None
        try:
            name = rhi.backendName()
        except Exception:  # pragma: no cover
            return "unknown"
        return name or None

    def output_path(self):
        backend = self.rhi_backend()
        if backend:
            return f"qt-multimedia-rhi/{backend} (yuv_triplanar shader, 1 YUV copy/frame)"
        return "qt-multimedia-cpu-conversion (no RHI backend active; software colour path if unsupported)"

    def close(self):
        if self._closed:
            return
        super().close()
        self._sink.setVideoFrame(QVideoFrame())
        self._surface.set_image(None)


class SoftwareVideoRenderer(Renderer):
    """Honest fallback: the old swscale BGRA conversion painted on a QWidget."""

    requested_mode = "software"
    mode = "software"

    def __init__(self):
        super().__init__()
        self._surface = _SoftwareSurface()
        self._widget = self._surface

    def prepare(self, frame, *, serial):
        colour = ColourSpec.from_av_frame(frame)
        rotation = display_rotation_from_frame(frame)
        image = software_image_from_av(frame, rotation=rotation)
        self._prepared_frames += 1
        return PreparedFrame(
            serial=serial,
            colour=colour,
            width=frame.width,
            height=frame.height,
            rotation=rotation,
            image=image,
        )

    def _render(self, prepared):
        image = prepared.image
        if image is None:
            image = software_image_from_av(prepared.source, rotation=prepared.rotation)
            prepared.image = image
        self._surface.set_image(image)
        self._software_route += 1

    def output_path(self):
        return "software (PyAV/swscale YUV->BGRA + QPainter)"


def probe_gpu():
    """(available, reason) for the Qt RHI path. Offscreen has no RHI backend."""
    try:
        QVideoWidget  # noqa: B018 - import already checked at module import
    except NameError:  # pragma: no cover
        return False, "Qt Multimedia widgets unavailable"
    app = QGuiApplication.instance()
    if app is None:
        return False, "no QGuiApplication: cannot host a Qt video window"
    platform = app.platformName()
    if platform in ("offscreen", "minimal", "minimalegl"):
        return False, f"Qt platform plugin '{platform}' has no RHI/GPU backend"
    return True, ""


def create_renderer(mode="auto", probe=None):
    """Build a renderer; ``auto`` only uses a path the probe allows, honestly labelled."""
    if mode not in ("auto", "gpu", "software"):
        raise ValueError(f"unknown renderer mode: {mode!r}")
    if mode == "software":
        return SoftwareVideoRenderer()
    available, reason = (probe or probe_gpu)()
    if available:
        return QtVideoRenderer()
    renderer = SoftwareVideoRenderer()
    renderer.requested_mode = mode
    renderer.fallback_reason = reason or "GPU path unavailable"
    return renderer


class FrameMailbox(QObject):
    """One-slot latest-ready-frame mailbox with coalesced notifications.

    ``put`` may be called from the decode thread; the ``changed`` signal is
    emitted once per batch on the GUI thread. Replacing/closing does not free a
    frame a consumer is still holding (Python references pin it).
    """

    wake = Signal()
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._serial = 0
        self._frame = None
        self._closed = False
        self.notification_pending = False
        self.wake.connect(self._dispatch, Qt.ConnectionType.QueuedConnection)

    @property
    def closed(self):
        return self._closed

    def put(self, frame):
        with self._lock:
            if self._closed:
                return None
            self._serial += 1
            serial = self._serial
            self._frame = frame
            notify = not self.notification_pending
            self.notification_pending = True
        if notify:
            self.wake.emit()
        return serial

    def latest(self):
        with self._lock:
            if self._frame is None:
                return None
            return self._serial, self._frame

    def clear(self):
        with self._lock:
            self._serial += 1
            self._frame = None
            self.notification_pending = False

    def close(self):
        if self._closed:
            return
        with self._lock:
            self._closed = True
            self.notification_pending = False

    @Slot()
    def _dispatch(self):
        with self._lock:
            if self._closed:
                self.notification_pending = False
                return
            self.notification_pending = False
        self.changed.emit()
