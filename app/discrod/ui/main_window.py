"""Main application window.

Layout:
  * Top toolbar: audio input (mic), audio output (virtual cable), MIDI port,
    and a Start/Stop engine button.
  * Left: the pad mapping table (note -> clip, channel, mode) with MIDI-learn.
  * Right: the mixer with one channel strip per audio channel + master.

The window owns the AudioEngine, MidiInput and Controller, and persists state
to the user config on close.
"""

from __future__ import annotations

import os

from PySide6 import QtCore, QtWidgets

from .. import audio as audio_mod
from ..audio import AudioEngine, list_devices
from ..audio.cables import pick_virtual_cable
from ..audio.clip import MODE_ONESHOT, MODE_GATE, MODE_LOOP, MODE_TOGGLE
from ..midi import MidiInput, list_ports, note_name
from ..mapping import PadBank, PadMapping, Controller
from ..config import AppConfig, load_config, save_config
from .channel_strip import ChannelStrip

MODES = [MODE_ONESHOT, MODE_GATE, MODE_LOOP, MODE_TOGGLE]


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Discrod — MIDI Soundboard & Virtual Mic")
        self.resize(1000, 600)

        self.cfg = load_config()
        self.engine = AudioEngine(self.cfg.sample_rate, self.cfg.block_size)
        if self.cfg.engine:
            self.engine.load_dict(self.cfg.engine)
        self.bank = PadBank()
        if self.cfg.bank:
            self.bank.load_dict(self.cfg.bank)
        self.controller = Controller(self.engine, self.bank)
        self.controller.on_error = self._on_clip_error
        self.engine.on_sample_rate_changed = self._on_sample_rate_changed
        self.midi = MidiInput(self._on_midi)

        self._build_ui()
        self._populate_devices()
        self._reload_pad_table()
        self._build_mixer()
        # Decode mapped clips up front so the first pad press never blocks the
        # GUI thread on file I/O.
        self.controller.preload_async()

        # Meter refresh timer.
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._refresh_meters)
        self._timer.start(50)

    # --- UI construction ----------------------------------------------------
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)

        # Toolbar row.
        bar = QtWidgets.QHBoxLayout()
        self.input_combo = QtWidgets.QComboBox()
        self.output_combo = QtWidgets.QComboBox()
        self.midi_combo = QtWidgets.QComboBox()
        bar.addWidget(QtWidgets.QLabel("Mic:"))
        bar.addWidget(self.input_combo, 1)
        bar.addWidget(QtWidgets.QLabel("Virtual mic out:"))
        bar.addWidget(self.output_combo, 1)
        bar.addWidget(QtWidgets.QLabel("MIDI:"))
        bar.addWidget(self.midi_combo, 1)
        self.refresh_btn = QtWidgets.QPushButton("⟳")
        self.refresh_btn.setToolTip("Rescan devices")
        self.refresh_btn.clicked.connect(self._populate_devices)
        bar.addWidget(self.refresh_btn)
        self.start_btn = QtWidgets.QPushButton("Start")
        self.start_btn.setCheckable(True)
        self.start_btn.toggled.connect(self._toggle_engine)
        bar.addWidget(self.start_btn)
        root.addLayout(bar)

        # Main split: pads (left) | mixer (right).
        split = QtWidgets.QSplitter()
        root.addWidget(split, 1)

        # Pads side.
        pads_widget = QtWidgets.QWidget()
        pads_layout = QtWidgets.QVBoxLayout(pads_widget)
        pads_layout.addWidget(QtWidgets.QLabel("Pad mappings"))
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Note", "Clip", "Channel", "Mode", "Gain dB"])
        self.table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.Stretch)
        pads_layout.addWidget(self.table, 1)

        pad_btns = QtWidgets.QHBoxLayout()
        add_btn = QtWidgets.QPushButton("Add pad…")
        add_btn.clicked.connect(self._add_pad)
        learn_btn = QtWidgets.QPushButton("MIDI learn")
        learn_btn.clicked.connect(self._learn_pad)
        del_btn = QtWidgets.QPushButton("Remove")
        del_btn.clicked.connect(self._remove_pad)
        test_btn = QtWidgets.QPushButton("Test play")
        test_btn.clicked.connect(self._test_selected)
        pad_btns.addWidget(add_btn)
        pad_btns.addWidget(learn_btn)
        pad_btns.addWidget(del_btn)
        pad_btns.addWidget(test_btn)
        pads_layout.addLayout(pad_btns)
        split.addWidget(pads_widget)

        # Mixer side.
        self.mixer_widget = QtWidgets.QWidget()
        self.mixer_layout = QtWidgets.QHBoxLayout(self.mixer_widget)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.mixer_widget)
        split.addWidget(scroll)
        split.setSizes([520, 480])

        self.statusBar().showMessage("Stopped")

    def _build_mixer(self):
        # Clear existing.
        while self.mixer_layout.count():
            item = self.mixer_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.strips = []
        for ch in self.engine.channels_list:
            strip = ChannelStrip(ch)
            self.strips.append(strip)
            self.mixer_layout.addWidget(strip)

        # Master strip (simple gain + meter).
        master = QtWidgets.QFrame()
        master.setFrameShape(QtWidgets.QFrame.StyledPanel)
        master.setFixedWidth(120)
        ml = QtWidgets.QVBoxLayout(master)
        lbl = QtWidgets.QLabel("Master")
        lbl.setAlignment(QtCore.Qt.AlignCenter)
        lbl.setStyleSheet("font-weight: bold;")
        ml.addWidget(lbl)
        self.master_fader = QtWidgets.QSlider(QtCore.Qt.Vertical)
        self.master_fader.setRange(-60, 12)
        self.master_fader.setValue(int(self.engine.master_gain_db))
        self.master_fader.setFixedHeight(160)
        self.master_fader.valueChanged.connect(
            lambda v: setattr(self.engine, "master_gain_db", float(v)))
        self.master_meter = QtWidgets.QProgressBar()
        self.master_meter.setOrientation(QtCore.Qt.Vertical)
        self.master_meter.setRange(0, 100)
        self.master_meter.setTextVisible(False)
        self.master_meter.setFixedHeight(160)
        mrow = QtWidgets.QHBoxLayout()
        mrow.addWidget(self.master_fader)
        mrow.addWidget(self.master_meter)
        ml.addLayout(mrow)
        ml.addStretch(1)
        self.mixer_layout.addWidget(master)
        self.mixer_layout.addStretch(1)

    # --- devices ------------------------------------------------------------
    def _populate_devices(self):
        inputs, outputs = list_devices()
        self.input_combo.clear()
        self.input_combo.addItem("(none)", None)
        for idx, name, api in inputs:
            label = f"{name} [{api}]" if api else name
            self.input_combo.addItem(label, (idx, name, api))
        self.output_combo.clear()
        self.output_combo.addItem("(default)", None)
        for idx, name, api in outputs:
            label = f"{name} [{api}]" if api else name
            self.output_combo.addItem(label, (idx, name, api))
        self.midi_combo.clear()
        self.midi_combo.addItem("(none)", None)
        for name in list_ports():
            self.midi_combo.addItem(name, name)
        self._select_saved_devices()

    def _select_saved_devices(self):
        # Audio devices are persisted as {name, hostapi}: PortAudio indices
        # shift across replug/reboot, and Windows lists the same device name
        # once per host API, so both fields are needed to re-identify the
        # selection.  Bare-name and integer values from older configs are
        # still accepted.
        def select_audio(combo, value):
            if value is None:
                return
            name = api = None
            if isinstance(value, dict):
                name, api = value.get("name"), value.get("hostapi")
            elif isinstance(value, str):
                name = value
            for i in range(combo.count()):
                data = combo.itemData(i)
                if data is None:
                    continue
                if isinstance(value, int):
                    if data[0] == value:
                        combo.setCurrentIndex(i)
                        return
                elif data[1] == name and (api is None or data[2] == api):
                    combo.setCurrentIndex(i)
                    return
            if api is not None:
                # Same name, different host API (e.g. the API list changed):
                # better than losing the device entirely.
                for i in range(combo.count()):
                    data = combo.itemData(i)
                    if data is not None and data[1] == name:
                        combo.setCurrentIndex(i)
                        return
            self.statusBar().showMessage(
                f"Saved device not found: {name or value}", 8000)

        def select_midi(combo, value):
            if value is None:
                return
            for i in range(combo.count()):
                if combo.itemData(i) == value:
                    combo.setCurrentIndex(i)
                    return
            self.statusBar().showMessage(
                f"Saved MIDI port not found: {value}", 8000)

        select_audio(self.input_combo, self.cfg.input_device)
        select_audio(self.output_combo, self.cfg.output_device)
        select_midi(self.midi_combo, self.cfg.midi_port)
        if self.cfg.output_device is None:
            self._auto_select_cable()

    def _auto_select_cable(self):
        # First run (no saved output device): if a known signed virtual cable
        # is already installed, select its render endpoint so the app works
        # without the user studying device names.  Any later explicit choice
        # is persisted and wins on subsequent launches.
        outputs = [self.output_combo.itemData(i)
                   for i in range(self.output_combo.count())
                   if self.output_combo.itemData(i) is not None]
        match = pick_virtual_cable(outputs)
        if match is None:
            return
        for i in range(self.output_combo.count()):
            data = self.output_combo.itemData(i)
            if data is not None and data[0] == match.device[0]:
                self.output_combo.setCurrentIndex(i)
                break
        self.statusBar().showMessage(
            f"Auto-selected {match.product.product} as virtual mic out — in "
            f"Discord, set the input device to {match.product.discord_mic}",
            15000)

    # --- engine -------------------------------------------------------------
    def _toggle_engine(self, on):
        if on:
            def device_index(combo):
                data = combo.currentData()
                return data[0] if data is not None else None
            try:
                self.engine.start(
                    input_device=device_index(self.input_combo),
                    output_device=device_index(self.output_combo),
                )
                port = self.midi_combo.currentData()
                if port:
                    self.midi.open(port)
                self.start_btn.setText("Stop")
                self.statusBar().showMessage("Running — routing to virtual mic")
            except Exception as exc:
                self.start_btn.setChecked(False)
                QtWidgets.QMessageBox.critical(self, "Engine error", str(exc))
        else:
            self.midi.close()
            self.engine.stop()
            self.start_btn.setText("Start")
            self.statusBar().showMessage("Stopped")

    # --- MIDI ---------------------------------------------------------------
    def _on_midi(self, event, note, velocity, channel):
        # Marshal to the GUI thread before touching the engine/controller.
        QtCore.QMetaObject.invokeMethod(
            self, "_dispatch_midi", QtCore.Qt.QueuedConnection,
            QtCore.Q_ARG(str, event), QtCore.Q_ARG(int, note),
            QtCore.Q_ARG(int, velocity), QtCore.Q_ARG(int, channel))

    @QtCore.Slot(str, int, int, int)
    def _dispatch_midi(self, event, note, velocity, channel):
        self.controller.handle_note(event, note, velocity, channel)

    # --- pad table ----------------------------------------------------------
    def _reload_pad_table(self):
        self.table.setRowCount(0)
        for note in sorted(self.bank.mappings):
            self._append_row(self.bank.mappings[note])

    def _append_row(self, mapping: PadMapping):
        row = self.table.rowCount()
        self.table.insertRow(row)
        note_item = QtWidgets.QTableWidgetItem(
            f"{mapping.note} ({note_name(mapping.note)})")
        note_item.setData(QtCore.Qt.UserRole, mapping.note)
        note_item.setFlags(note_item.flags() & ~QtCore.Qt.ItemIsEditable)
        self.table.setItem(row, 0, note_item)
        clip_item = QtWidgets.QTableWidgetItem(os.path.basename(mapping.clip_path))
        clip_item.setToolTip(mapping.clip_path)
        clip_item.setFlags(clip_item.flags() & ~QtCore.Qt.ItemIsEditable)
        self.table.setItem(row, 1, clip_item)

        chan_combo = QtWidgets.QComboBox()
        for ch in self.engine.channels_list:
            chan_combo.addItem(ch.name)
        chan_combo.setCurrentText(mapping.channel)
        if chan_combo.currentText() != mapping.channel:
            # Mapping referenced a channel that no longer exists (stale/edited
            # config).  Fall back to the soundboard bus — the same fallback
            # trigger_clip applies — and show it, so the UI and the trigger
            # path agree (a failed setCurrentText leaves index 0, which is the
            # Microphone strip and would route the clip through the mic chain).
            mapping.channel = self.engine.soundboard_channel.name
            chan_combo.setCurrentText(mapping.channel)
        chan_combo.currentTextChanged.connect(
            lambda v, n=mapping.note: self._update_mapping(n, channel=v))
        self.table.setCellWidget(row, 2, chan_combo)

        mode_combo = QtWidgets.QComboBox()
        mode_combo.addItems(MODES)
        mode_combo.setCurrentText(mapping.mode)
        mode_combo.currentTextChanged.connect(
            lambda v, n=mapping.note: self._update_mapping(n, mode=v))
        self.table.setCellWidget(row, 3, mode_combo)

        gain_spin = QtWidgets.QDoubleSpinBox()
        gain_spin.setRange(-60, 12)
        gain_spin.setValue(mapping.gain_db)
        gain_spin.valueChanged.connect(
            lambda v, n=mapping.note: self._update_mapping(n, gain_db=v))
        self.table.setCellWidget(row, 4, gain_spin)

    def _update_mapping(self, note, **kwargs):
        mapping = self.bank.get(note)
        if mapping is None:
            return
        for k, v in kwargs.items():
            setattr(mapping, k, v)

    def _choose_clip(self) -> str | None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Choose audio clip", "",
            "Audio (*.wav *.flac *.ogg *.aiff *.mp3);;All files (*)")
        return path or None

    def _add_pad(self):
        note, ok = QtWidgets.QInputDialog.getInt(
            self, "Add pad", "MIDI note number (0-127):", 60, 0, 127)
        if not ok:
            return
        path = self._choose_clip()
        if not path:
            return
        mapping = PadMapping(note=note, clip_path=path,
                             channel=self.engine.channels_list[-1].name)
        self.bank.set(mapping)
        self._reload_pad_table()
        self.controller.preload_async()

    def _learn_pad(self):
        if self.midi.learn_callback is not None:
            # Second click cancels — a stale armed learn would swallow the
            # first pad press of the next session and silently rebind it.
            self.midi.cancel_learn()
            self.statusBar().showMessage("MIDI learn cancelled")
            return
        if self.midi.port_name is None:
            port = self.midi_combo.currentData()
            if not port:
                self.statusBar().showMessage(
                    "Select a MIDI port before using MIDI learn")
                return
            try:
                self.midi.open(port)
            except Exception as exc:
                QtWidgets.QMessageBox.critical(self, "MIDI error", str(exc))
                return
        path = self._choose_clip()
        if not path:
            return
        self.statusBar().showMessage(
            "MIDI learn: press a key on your controller… (click again to cancel)")
        self.midi.arm_learn(lambda n: QtCore.QMetaObject.invokeMethod(
            self, "_finish_learn", QtCore.Qt.QueuedConnection,
            QtCore.Q_ARG(int, n), QtCore.Q_ARG(str, path)))

    @QtCore.Slot(int, str)
    def _finish_learn(self, note, path):
        mapping = PadMapping(note=note, clip_path=path,
                             channel=self.engine.channels_list[-1].name)
        self.bank.set(mapping)
        self._reload_pad_table()
        self.controller.preload_async()
        self.statusBar().showMessage(f"Bound note {note} ({note_name(note)})")

    def _remove_pad(self):
        row = self.table.currentRow()
        if row < 0:
            return
        note = self.table.item(row, 0).data(QtCore.Qt.UserRole)
        self.bank.remove(note)
        self._reload_pad_table()

    def _test_selected(self):
        row = self.table.currentRow()
        if row < 0:
            return
        if not self.engine.running:
            self.statusBar().showMessage(
                "Engine is stopped — press Start to hear pads", 5000)
            return
        note = self.table.item(row, 0).data(QtCore.Qt.UserRole)
        self.controller.handle_note("note_on", note, 100, 0)

    # --- engine/controller callbacks ---------------------------------------
    def _on_clip_error(self, note, message):
        # May fire from the preload thread; marshal to the GUI thread.
        QtCore.QMetaObject.invokeMethod(
            self, "_show_status", QtCore.Qt.QueuedConnection,
            QtCore.Q_ARG(str, message))

    @QtCore.Slot(str)
    def _show_status(self, message):
        self.statusBar().showMessage(message, 8000)

    def _on_sample_rate_changed(self, sample_rate):
        # Called from _toggle_engine (GUI thread) during device negotiation.
        # Cached clips were decoded at the old rate and must be reloaded.
        self.controller.invalidate_cache()
        self.controller.preload_async()
        self.statusBar().showMessage(
            f"Devices negotiated {sample_rate} Hz — clips reloaded", 8000)

    # --- meters / lifecycle -------------------------------------------------
    def _refresh_meters(self):
        for strip in self.strips:
            strip.refresh_meter()
        peak = self.engine.master_meter
        if peak <= 0:
            db = -60.0
        else:
            import numpy as np
            db = max(-60.0, 20.0 * float(np.log10(peak)))
        self.master_meter.setValue(int((db + 60) / 60 * 100))

    def closeEvent(self, event):
        self.midi.close()
        self.engine.stop()

        def device_value(combo):
            # "(none)"/"(default)" carry data None -> persist None; otherwise
            # persist {name, hostapi} — stable across replug/reboot, unlike
            # the PortAudio index, and unambiguous across Windows host APIs,
            # unlike the bare name.
            data = combo.currentData()
            if data is None:
                return None
            return {"name": data[1], "hostapi": data[2]}

        self.cfg.input_device = device_value(self.input_combo)
        self.cfg.output_device = device_value(self.output_combo)
        self.cfg.midi_port = self.midi_combo.currentData()
        self.cfg.engine = self.engine.to_dict()
        self.cfg.bank = self.bank.to_dict()
        try:
            save_config(self.cfg)
        except Exception:
            pass
        super().closeEvent(event)
