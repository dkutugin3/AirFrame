"""Wheel gestures scroll settings without silently changing their values."""

import unittest

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QComboBox, QLabel, QScrollArea, QSlider, QVBoxLayout, QWidget,
)

try:
    from tests_native import qt_harness
except ImportError:
    import qt_harness

from airframe.widgets import WheelSafeComboBox, WheelSafeSlider


def wheel(widget, *, angle=-120, pixels=0):
    center = widget.rect().center()
    event = QWheelEvent(
        QPointF(center), QPointF(widget.mapToGlobal(center)),
        QPoint(0, pixels), QPoint(0, angle),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate, False,
    )
    QApplication.sendEvent(widget, event)
    QApplication.processEvents()
    return event


def scroll_at(widget, *, angle=-120, pixels=0):
    """Deliver through the window so Qt performs normal parent propagation."""
    window = widget.window()
    position = widget.mapTo(window, widget.rect().center())
    QTest.wheelEvent(window.windowHandle(), position,
                     QPoint(0, angle), QPoint(0, pixels))
    QApplication.processEvents()


class WheelControlTests(unittest.TestCase):
    def setUp(self):
        qt_harness.qt_app()
        self.host = QWidget()
        self.layout = QVBoxLayout(self.host)

    def tearDown(self):
        self.host.close()
        self.host.deleteLater()
        QApplication.processEvents()

    def combo(self):
        combo = WheelSafeComboBox()
        combo.addItems([str(i) for i in range(40)])
        combo.setCurrentIndex(15)
        self.layout.addWidget(combo)
        self.host.show()
        QApplication.processEvents()
        return combo

    def test_closed_focused_combo_ignores_mouse_and_trackpad_wheels(self):
        combo = self.combo()
        combo.setFocus()
        changes = []
        combo.currentIndexChanged.connect(changes.append)
        for angle, pixels in ((120, 0), (-120, 0), (0, -30), (0, 30)):
            event = wheel(combo, angle=angle, pixels=pixels)
            self.assertFalse(event.isAccepted())
            self.assertEqual(combo.currentIndex(), 15)
        self.assertEqual(changes, [])
        QTest.keyClick(combo, Qt.Key.Key_Down)
        self.assertEqual(combo.currentIndex(), 16)

    def test_open_dropdown_can_scroll_and_select(self):
        combo = self.combo()
        combo.setMaxVisibleItems(5)
        combo.showPopup()
        QApplication.processEvents()
        view = combo.view()
        self.assertTrue(view.isVisible())
        scrollbar = view.verticalScrollBar()
        scrollbar.setValue(scrollbar.minimum())
        before = scrollbar.value()
        scroll_at(view.viewport(), angle=-120)
        self.assertGreater(scrollbar.value(), before)
        # Pick a visible item with a click after scrolling the popup.
        position = view.viewport().rect().center()
        index = view.indexAt(position)
        self.assertTrue(index.isValid())
        expected = index.row()
        QTest.mouseMove(view.viewport(), position)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=position)
        self.assertEqual(combo.currentIndex(), expected)
        self.assertFalse(view.isVisible())

    def test_slider_ignores_wheel_but_keeps_keyboard_and_clicks(self):
        slider = WheelSafeSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(50)
        self.layout.addWidget(slider)
        self.host.show()
        slider.setFocus()
        QApplication.processEvents()
        changes = []
        slider.valueChanged.connect(changes.append)
        for angle, pixels in ((120, 0), (-120, 0), (0, -30)):
            self.assertFalse(wheel(slider, angle=angle, pixels=pixels).isAccepted())
            self.assertEqual(slider.value(), 50)
        self.assertEqual(changes, [])
        QTest.keyClick(slider, Qt.Key.Key_Right)
        self.assertEqual(slider.value(), 51)
        QTest.mouseClick(slider, Qt.MouseButton.LeftButton,
                         pos=QPoint(slider.width() - 4, slider.height() // 2))
        self.assertGreater(slider.value(), 51)

    def test_wheel_over_controls_scrolls_parent(self):
        area = QScrollArea()
        area.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        combo = WheelSafeComboBox()
        combo.addItems(["30 fps", "60 fps", "120 fps"])
        combo.setCurrentIndex(1)
        slider = WheelSafeSlider(Qt.Orientation.Horizontal)
        slider.setValue(50)
        layout.addWidget(combo)
        layout.addWidget(slider)
        filler = QLabel("Settings")
        filler.setMinimumHeight(1600)
        layout.addWidget(filler)
        area.setWidget(content)
        self.layout.addWidget(area)
        self.host.resize(320, 300)
        self.host.show()
        QApplication.processEvents()
        for control in (combo, slider):
            area.verticalScrollBar().setValue(0)
            control.setFocus()
            scroll_at(control)
            self.assertGreater(area.verticalScrollBar().value(), 0)
        self.assertEqual(combo.currentIndex(), 1)
        self.assertEqual(slider.value(), 50)

    def test_desktop_settings_use_wheel_safe_controls(self):
        window = qt_harness.Desktop(qt_harness.StubServer())
        try:
            combos = window.findChildren(QComboBox)
            sliders = window.findChildren(QSlider)
            self.assertGreaterEqual(len(combos), 5)
            self.assertGreaterEqual(len(sliders), 2)
            for control in combos:
                self.assertIsInstance(control, WheelSafeComboBox)
            for control in sliders:
                self.assertIsInstance(control, WheelSafeSlider)
        finally:
            qt_harness.close_window(window)


if __name__ == "__main__":
    unittest.main()
