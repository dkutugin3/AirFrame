"""Stage 5a native surface: sender telemetry must reach the diagnostics text.

Run with a Qt-capable interpreter (Docker's tests/ image has no PySide6):

    sh scripts/setup-native-tests.sh
    PYTHONPATH="$PWD/.superpowers/native-deps" QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests_native -v

The stats are injected through the real Events signal the receiver uses, so this
checks the desktop diagnostics surface, not only pure formatting helpers.
"""
import unittest

try:  # package import when discovered with top_level_dir=repo root
    from tests_native import qt_harness
except ImportError:  # discovery imports test modules as top-level names
    import qt_harness

from airframe.desktop import Desktop


def legacy_stats(**overrides):
    """A complete receiver stats payload with the legacy sender telemetry keys."""
    stats = {
        "fps":59.0,"bitrate":18.0,"jitter":2.0,"loss":0.0,
        "convertedFps":59.0,"skipped":0,"conversionMs":2.0,"frameGapMs":17.0,
        "playoutMode":"stable","playoutTargetMs":250,"playoutQueuedFrames":13,
        "playoutUnderflows":0,"playoutRebuffers":0,"outputGapMs":17.0,
        "packetGapMs":20.0,"assembledFps":60.0,"assemblyGapMs":20.0,
        "largestFrameKiB":150.0,"bufferRecoveries":0,
        "encodedFps":60,"width":2560,"height":1440,"limitation":"none",
    }
    stats.update(overrides)
    return stats


TELEMETRY = {"captureFps":59.5,"maxCaptureIntervalMs":34.2,"pacerFps":60,
             "framesEncoded":12000,"framesSent":11950,"videoBitrateKbps":21500,
             "avgEncodeTimeMs":7.4,"codec":"H264","codecProfile":"42e01f",
             "encoderImplementation":"c2.android.avc.encoder","hardwareEncoder":"unknown",
             "thermalStatus":"none","captureWidth":2560,"captureHeight":1440,
             "telemetryWindowMs":1000,"sampledMonotonicMs":123456}


class TelemetrySurfaceTests(unittest.TestCase):
    def setUp(self):
        qt_harness.qt_app()
        self.server = qt_harness.StubServer()
        self.window = Desktop(self.server)
        self.window.show()
        self.now_ms = 1000
        self.encoded = 0
        self.sent = 0

    def tearDown(self):
        qt_harness.close_window(self.window)

    def emit(self, **overrides):
        self.server.events.stats.emit(legacy_stats(**overrides))

    def phone_sample(self, capture_fps=60, pacer_fps=60, encoded_fps=60, **overrides):
        """Emit one increasing telemetry sample ~1 s after the previous one."""
        self.now_ms += 1000
        self.encoded += 60
        self.sent += 60
        self.emit(captureFps=capture_fps,pacerFps=pacer_fps,encodedFps=encoded_fps,
                  framesEncoded=self.encoded,framesSent=self.sent,
                  sampledMonotonicMs=self.now_ms,telemetryWindowMs=1000,
                  **overrides)

    def diagnostics(self):
        return self.window.stream_diag.toPlainText() + "\n" + self.window.journal.toPlainText()

    def test_telemetry_diagnostics_show_measurement_chain(self):
        self.emit(**TELEMETRY)  # first sample: derived rates are unknown
        self.emit(**{**TELEMETRY,"framesEncoded":12060,"framesSent":12010,
                     "sampledMonotonicMs":124456})
        text = self.diagnostics()
        for expected in ("Телеметрия","захват 59.5","pacer 60","кодировщик 60",
                         "отправлено","21.5 Мбит/с","среднее время кодирования за окно",
                         "H264","42e01f","2560×1440","аппаратный кодировщик: неизвестно",
                         "температура: нет перегрева"):
            self.assertIn(expected,text)
        self.assertNotIn("p95",text)

    def test_telemetry_absent_values_are_unknown_not_zero(self):
        self.emit()  # legacy-only telemetry: no new optional fields at all
        text = self.diagnostics()
        self.assertIn("температура: неизвестно",text)
        self.assertIn("аппаратный кодировщик: неизвестно",text)
        self.assertNotIn("температура: 0",text)
        self.assertNotIn("аппаратный кодировщик: 0",text)

    def test_telemetry_zero_is_shown_as_measured_zero(self):
        self.emit(captureFps=0,videoBitrateKbps=0)
        text = self.diagnostics()
        self.assertIn("захват 0",text)
        self.assertIn("0 кбит/с",text)

    def test_telemetry_bottlenecks_are_distinguished(self):
        # Healthy chain: every stage keeps up with the 60 fps session target.
        self.phone_sample()
        self.phone_sample(fps=59.0)
        self.assertIn("ограничений не видно",self.diagnostics())
        # Capture did not produce 60 fps, so everything downstream is short too.
        self.phone_sample(capture_fps=40,pacer_fps=40,encoded_fps=40,fps=40.0)
        self.assertIn("захват не дал 60",self.diagnostics())
        # Capture is fine but the pacer/encoder are late.
        self.phone_sample(capture_fps=60,pacer_fps=45,encoded_fps=45,fps=45.0)
        self.assertIn("кодировщик не успевает",self.diagnostics())
        # Phone still sends 60 fps but the PC receives/decodes fewer frames.
        self.phone_sample(capture_fps=60,pacer_fps=60,encoded_fps=60,fps=41.0)
        self.assertIn("транспорт/ПК не успевают",self.diagnostics())


if __name__ == "__main__":
    unittest.main()
