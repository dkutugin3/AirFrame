"""Settings controls that leave wheel gestures to the surrounding scroll area."""

from PySide6.QtWidgets import QComboBox, QSlider


class WheelSafeComboBox(QComboBox):
    """Require an explicit selection, even when the closed control has focus."""

    def wheelEvent(self, event):
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            event.ignore()


class WheelSafeSlider(QSlider):
    """Keep click, drag and keyboard input while passing wheel gestures through."""

    def wheelEvent(self, event):
        event.ignore()
