"""A mixer channel strip widget: fader, mute/solo, meter and DSP toggles.

Clicking a DSP button opens a small editor dialog for that processor's
parameters.  All edits write directly to the live engine objects, so changes are
heard immediately.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ..audio.dsp.gain import linear_to_db


class _ParamSlider(QtWidgets.QWidget):
    """Labelled horizontal slider mapping to a float range."""

    def __init__(self, label, minimum, maximum, value, step=1.0, suffix="",
                 on_change=None):
        super().__init__()
        self._min = minimum
        self._max = maximum
        self._step = step
        self._suffix = suffix
        self._on_change = on_change
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._label = QtWidgets.QLabel(label)
        self._label.setMinimumWidth(70)
        self._slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self._slider.setMinimum(0)
        self._slider.setMaximum(int((maximum - minimum) / step))
        self._slider.setValue(int((value - minimum) / step))
        self._value_label = QtWidgets.QLabel()
        self._value_label.setMinimumWidth(60)
        self._value_label.setAlignment(QtCore.Qt.AlignRight)
        layout.addWidget(self._label)
        layout.addWidget(self._slider, 1)
        layout.addWidget(self._value_label)
        self._slider.valueChanged.connect(self._changed)
        self._update_label(value)

    def _value(self) -> float:
        return self._min + self._slider.value() * self._step

    def _changed(self, _):
        val = self._value()
        self._update_label(val)
        if self._on_change:
            self._on_change(val)

    def _update_label(self, val):
        self._value_label.setText(f"{val:.1f}{self._suffix}")


class DspEditor(QtWidgets.QDialog):
    """Editor for a channel's gate, compressor and EQ."""

    def __init__(self, channel, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{channel.name} — Processing")
        self.channel = channel
        layout = QtWidgets.QVBoxLayout(self)
        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._gate_tab(channel.gate), "Gate")
        tabs.addTab(self._comp_tab(channel.compressor), "Compressor")
        tabs.addTab(self._eq_tab(channel.eq), "EQ")
        layout.addWidget(tabs)
        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        btns.rejected.connect(self.accept)
        btns.accepted.connect(self.accept)
        layout.addWidget(btns)

    def _enable_row(self, proc):
        cb = QtWidgets.QCheckBox("Enabled")
        cb.setChecked(proc.enabled)
        cb.toggled.connect(lambda v: setattr(proc, "enabled", v))
        return cb

    def _gate_tab(self, gate):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.addWidget(self._enable_row(gate))
        lay.addWidget(_ParamSlider("Threshold", -80, 0, gate.threshold_db, 0.5,
                                   " dB", lambda v: setattr(gate, "threshold_db", v)))
        lay.addWidget(_ParamSlider("Range", -80, 0, gate.range_db, 0.5, " dB",
                                   lambda v: setattr(gate, "range_db", v)))
        lay.addWidget(_ParamSlider("Attack", 0.1, 50, gate.attack_ms, 0.1, " ms",
                                   lambda v: setattr(gate, "attack_ms", v)))
        lay.addWidget(_ParamSlider("Hold", 0, 500, gate.hold_ms, 1, " ms",
                                   lambda v: setattr(gate, "hold_ms", v)))
        lay.addWidget(_ParamSlider("Release", 1, 1000, gate.release_ms, 1, " ms",
                                   lambda v: setattr(gate, "release_ms", v)))
        lay.addStretch(1)
        return w

    def _comp_tab(self, comp):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.addWidget(self._enable_row(comp))
        lay.addWidget(_ParamSlider("Threshold", -60, 0, comp.threshold_db, 0.5,
                                   " dB", lambda v: setattr(comp, "threshold_db", v)))
        lay.addWidget(_ParamSlider("Ratio", 1, 20, comp.ratio, 0.1, ":1",
                                   lambda v: setattr(comp, "ratio", v)))
        lay.addWidget(_ParamSlider("Attack", 0.1, 100, comp.attack_ms, 0.1, " ms",
                                   lambda v: setattr(comp, "attack_ms", v)))
        lay.addWidget(_ParamSlider("Release", 5, 1000, comp.release_ms, 1, " ms",
                                   lambda v: setattr(comp, "release_ms", v)))
        lay.addWidget(_ParamSlider("Make-up", 0, 24, comp.makeup_db, 0.5, " dB",
                                   lambda v: setattr(comp, "makeup_db", v)))
        lay.addStretch(1)
        return w

    def _eq_tab(self, eq):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.addWidget(self._enable_row(eq))
        names = ["Low", "Mid", "High"]
        for i, band in enumerate(eq.bands):
            label = names[i] if i < len(names) else f"Band {i+1}"
            box = QtWidgets.QGroupBox(f"{label} ({band.ftype})")
            bl = QtWidgets.QVBoxLayout(box)
            bl.addWidget(_ParamSlider("Freq", 20, 18000, band.freq, 1, " Hz",
                                      lambda v, idx=i: eq.set_band_freq(idx, v)))
            bl.addWidget(_ParamSlider("Gain", -18, 18, band.gain_db, 0.5, " dB",
                                      lambda v, idx=i: eq.set_band_gain(idx, v)))
            bl.addWidget(_ParamSlider("Q", 0.1, 8, band.q, 0.1, "",
                                      lambda v, idx=i: eq.set_band_q(idx, v)))
            lay.addWidget(box)
        lay.addStretch(1)
        return w


class ChannelStrip(QtWidgets.QFrame):
    def __init__(self, channel, parent=None):
        super().__init__(parent)
        self.channel = channel
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setFixedWidth(120)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(4)

        title = QtWidgets.QLabel(channel.name)
        title.setAlignment(QtCore.Qt.AlignCenter)
        title.setStyleSheet("font-weight: bold;")
        layout.addWidget(title)

        self.meter = QtWidgets.QProgressBar()
        self.meter.setOrientation(QtCore.Qt.Vertical)
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.meter.setFixedHeight(160)

        self.fader = QtWidgets.QSlider(QtCore.Qt.Vertical)
        self.fader.setRange(-60, 12)
        self.fader.setValue(int(channel.gain_db))
        self.fader.setFixedHeight(160)
        self.fader.valueChanged.connect(self._gain_changed)

        meter_row = QtWidgets.QHBoxLayout()
        meter_row.addWidget(self.fader)
        meter_row.addWidget(self.meter)
        layout.addLayout(meter_row)

        self.gain_label = QtWidgets.QLabel(f"{channel.gain_db:.0f} dB")
        self.gain_label.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self.gain_label)

        btn_row = QtWidgets.QHBoxLayout()
        self.mute_btn = QtWidgets.QPushButton("M")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setChecked(channel.mute)
        self.mute_btn.toggled.connect(lambda v: setattr(channel, "mute", v))
        self.solo_btn = QtWidgets.QPushButton("S")
        self.solo_btn.setCheckable(True)
        self.solo_btn.setChecked(channel.solo)
        self.solo_btn.toggled.connect(lambda v: setattr(channel, "solo", v))
        btn_row.addWidget(self.mute_btn)
        btn_row.addWidget(self.solo_btn)
        layout.addLayout(btn_row)

        self.dsp_btn = QtWidgets.QPushButton("FX…")
        self.dsp_btn.clicked.connect(self._open_dsp)
        layout.addWidget(self.dsp_btn)
        layout.addStretch(1)

    def _gain_changed(self, value):
        self.channel.gain_db = float(value)
        self.gain_label.setText(f"{value} dB")

    def _open_dsp(self):
        DspEditor(self.channel, self).exec()

    def refresh_meter(self):
        # Convert linear peak to a 0..100 scale (-60..0 dB).
        peak = self.channel.meter
        if peak <= 0:
            db = -60.0
        else:
            db = max(-60.0, linear_to_db(peak))
        self.meter.setValue(int((db + 60) / 60 * 100))
