"""MaxGaffer dock — PySide6, instrument-grade dark (the LightMatch/MaxDirector house style).

Layout: camera board on the left (pick a shot, see its reference + score), work column on
the right (reference, match loop, rig sliders, Vantage). Threading contract:
  * every pymxs touch happens on Max's MAIN thread — always;
  * slow pure-I/O (gateway calls, sidecar stats) runs on a QThread while the main thread
    spins a local QEventLoop, so Max stays responsive mid-match and Cancel always works;
  * renders block Max by nature — the log narrates so it never feels dead.

Loaded inside 3ds Max only (bootstrap checks deps first).
"""

from __future__ import annotations

import html as _html
import os
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore

from ..core.genome import GROUP_PREFIX, LightingState, spec_for
from ..core.omega import OmegaError, ping
from ..maxbridge import config as cfgmod
from ..maxbridge.controller import Controller

ACCENT = "#c6bfff"
BG = "#0e0e12"
PANEL = "#16161c"
ERR = "#ff6b6b"
OK = "#7ddba3"

STYLE = (
    f"QWidget{{background:{BG};color:#eceaf4;font-family:Inter,'Segoe UI';font-size:13px;}}"
    f"QPushButton{{background:{PANEL};border:1px solid #2b2b36;padding:9px 14px;"
    f"border-radius:8px;}}"
    f"QPushButton:hover{{border-color:{ACCENT};color:#ffffff;}}"
    f"QPushButton:disabled{{color:#5a5a66;border-color:#20202a;}}"
    f"QPushButton#primary{{background:{ACCENT};color:#12121a;font-weight:600;"
    f"min-height:26px;letter-spacing:1px;}}"
    f"QPushButton#danger{{border-color:{ERR};color:{ERR};}}"
    f"QPushButton#chip{{padding:5px 10px;border-radius:12px;color:#b9b4d6;font-size:12px;}}"
    f"QLineEdit,QComboBox,QPlainTextEdit,QTextEdit,QTreeWidget,QListWidget,QSpinBox,"
    f"QDoubleSpinBox"
    f"{{background:{PANEL};border:1px solid #2b2b36;border-radius:8px;padding:6px;"
    f"selection-background-color:{ACCENT};selection-color:#12121a;}}"
    f"QGroupBox{{border:1px solid #26262f;border-radius:12px;margin-top:18px;"
    f"padding:16px 12px 12px 12px;font-weight:600;letter-spacing:2px;}}"
    f"QGroupBox::title{{color:{ACCENT};subcontrol-origin:margin;left:14px;padding:0 6px;}}"
    f"QSlider::groove:horizontal{{height:4px;background:#26262f;border-radius:2px;}}"
    f"QSlider::handle:horizontal{{width:14px;background:{ACCENT};margin:-6px 0;"
    f"border-radius:7px;}}"
    f"QLabel#dim{{color:#9a97b3;}}"
    f"QScrollBar:vertical{{background:transparent;width:10px;}}"
    f"QScrollBar::handle:vertical{{background:#2b2b36;border-radius:5px;min-height:30px;}}"
    f"QScrollBar::add-line,QScrollBar::sub-line{{height:0;}}"
)


class _Worker(QtCore.QThread):
    done = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn())
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class _ProgressRelay(QtCore.QObject):
    """Marshals worker-thread progress callbacks onto the main thread — Qt widgets must
    never be touched from a vantage_console watcher thread."""

    progress = QtCore.Signal(str, str)


class MaxGafferDock(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MaxGaffer")
        self.setStyleSheet(STYLE)
        self.cfg = cfgmod.load()
        self.ctrl = Controller(self.cfg)
        self.ctrl.io = self._run_blocking_io   # gateway waits run off-thread, Max stays alive
        self._workers: List[_Worker] = []
        self._cancel = False
        self._busy = False
        self._sliders: Dict[str, QtWidgets.QDoubleSpinBox] = {}
        self._build()
        self.refresh_cameras()
        self._recover_draft_snapshot()

    def _recover_draft_snapshot(self):
        """A leftover snapshot means Max died mid-match with draft settings applied —
        put the artist's render settings back before anything else happens."""
        try:
            from ..maxbridge import draft as df

            if df.pending_snapshot():
                self._log("⚠ recovering render settings from a previous crashed session:")
                for line in df.restore_draft():
                    self._log("  " + line)
        except Exception as e:  # noqa: BLE001
            self._log(f"draft recovery check failed: {e}")

    # ================================================================= layout
    def _build(self):
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(16)

        # ---------------- left: camera board
        left = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel("MAXGAFFER")
        title.setStyleSheet(f"color:{ACCENT};font-weight:700;letter-spacing:4px;font-size:14px;")
        left.addWidget(title)
        sub = QtWidgets.QLabel("reference-matched lighting · V-Ray 7 · Vantage")
        sub.setObjectName("dim")
        left.addWidget(sub)

        self.cam_tree = QtWidgets.QTreeWidget()
        self.cam_tree.setHeaderLabels(["camera", "ref", "score"])
        self.cam_tree.setRootIsDecorated(False)
        self.cam_tree.setColumnWidth(0, 170)
        self.cam_tree.setColumnWidth(1, 36)
        self.cam_tree.currentItemChanged.connect(self._on_camera_selected)
        left.addWidget(self.cam_tree, 1)

        row = QtWidgets.QHBoxLayout()
        btn_refresh = QtWidgets.QPushButton("Refresh")
        btn_refresh.clicked.connect(self.refresh_cameras)
        row.addWidget(btn_refresh)
        self.chk_apply_on_select = QtWidgets.QCheckBox("apply saved light on select")
        self.chk_apply_on_select.setChecked(True)
        self.chk_apply_on_select.toggled.connect(self._on_apply_on_select)
        row.addWidget(self.chk_apply_on_select)
        left.addLayout(row)

        btn_settings = QtWidgets.QPushButton("Settings…")
        btn_settings.clicked.connect(self._open_settings)
        left.addWidget(btn_settings)
        root.addLayout(left, 0)

        # ---------------- right: work column (scrollable)
        right_host = QtWidgets.QScrollArea()
        right_host.setWidgetResizable(True)
        right_host.setFrameShape(QtWidgets.QFrame.NoFrame)
        right_w = QtWidgets.QWidget()
        right = QtWidgets.QVBoxLayout(right_w)
        right.setSpacing(14)
        right_host.setWidget(right_w)
        root.addWidget(right_host, 1)

        # reference group — reference vs latest match, side by side
        g_ref = QtWidgets.QGroupBox("REFERENCE  ·  LATEST MATCH")
        lr = QtWidgets.QHBoxLayout(g_ref)
        lr.setSpacing(14)

        def _thumb(placeholder):
            t = QtWidgets.QLabel(placeholder)
            t.setFixedSize(272, 153)
            t.setAlignment(QtCore.Qt.AlignCenter)
            t.setStyleSheet(f"background:{PANEL};border:1px dashed #2b2b36;"
                            "border-radius:10px;color:#5a5a66;")
            return t

        self.ref_thumb = _thumb("no reference")
        lr.addWidget(self.ref_thumb)
        self.match_thumb = _thumb("no match yet")
        lr.addWidget(self.match_thumb)
        ref_col = QtWidgets.QVBoxLayout()
        ref_col.setSpacing(10)
        btn_ref = QtWidgets.QPushButton("Load / swap reference…")
        btn_ref.clicked.connect(self._pick_reference)
        ref_col.addWidget(btn_ref)
        self.lbl_ref_info = QtWidgets.QLabel("")
        self.lbl_ref_info.setObjectName("dim")
        self.lbl_ref_info.setWordWrap(True)
        ref_col.addWidget(self.lbl_ref_info, 1)
        lr.addLayout(ref_col, 1)
        right.addWidget(g_ref)

        # match group
        g_match = QtWidgets.QGroupBox("MATCH")
        lm = QtWidgets.QVBoxLayout(g_match)
        prow = QtWidgets.QHBoxLayout()
        self.chk_plan = QtWidgets.QCheckBox("scene-wide plan first")
        self.chk_plan.setChecked(bool(self.cfg.plan_first))
        self.chk_plan.setToolTip(
            "Reads EVERY current setting (renderer, environment, exposure, all lights, "
            "cameras), compares the scene to the reference, and writes an explicit change "
            "plan — it may adjust any existing property and CREATE new lights (MG_ layer). "
            "You preview the plan before it executes; one Ctrl+Z reverts the whole plan.")
        prow.addWidget(self.chk_plan)
        self.chk_auto_exec = QtWidgets.QCheckBox("auto-execute plan")
        self.chk_auto_exec.setChecked(bool(self.cfg.auto_execute_plan))
        self.chk_auto_exec.setToolTip("Skip the preview dialog and execute immediately.")
        prow.addWidget(self.chk_auto_exec)
        prow.addStretch(1)
        lm.addLayout(prow)
        opts = QtWidgets.QHBoxLayout()
        self.chk_sweep = QtWidgets.QCheckBox("sun sweep first")
        self.chk_sweep.setChecked(True)   # a wrong sun direction wastes the whole run;
        self.chk_sweep.setToolTip(        # 8 low-res renders are cheap insurance
            "Grid-render 8 sun directions and let the model pick before iterating — the "
            "robust solve for sun azimuth. Uncheck on very heavy scenes to save renders.")
        opts.addWidget(self.chk_sweep)
        opts.addWidget(QtWidgets.QLabel("iterations"))
        self.spin_iters = QtWidgets.QSpinBox()
        self.spin_iters.setRange(1, 12)
        self.spin_iters.setValue(int(self.cfg.max_iterations))
        opts.addWidget(self.spin_iters)
        opts.addWidget(QtWidgets.QLabel("target"))
        self.spin_target = QtWidgets.QDoubleSpinBox()
        self.spin_target.setRange(50.0, 100.0)
        self.spin_target.setValue(float(self.cfg.target_score))
        opts.addWidget(self.spin_target)
        self.chk_deep = QtWidgets.QCheckBox("deep match → 99")
        self.chk_deep.setToolTip(
            "Hero-shot mode: up to 10 iterations targeting 99, then an LLM-free "
            "coordinate-descent polish that keeps rendering fine nudges until no move "
            "improves the score — a proven optimum. If 99 isn't reachable, the report says "
            "the remaining gap is content, not lighting. Costs ~20-40 extra loop renders.")
        opts.addWidget(self.chk_deep)
        self.chk_draft = QtWidgets.QCheckBox("draft sampler")
        self.chk_draft.setChecked(bool(self.cfg.draft_sampler))
        self.chk_draft.setToolTip(
            "OPT-IN: apply draft sampler settings (noise threshold / subdivs / time cap) "
            "during the match, restored automatically afterwards — crash-safe snapshot on "
            "disk. Never touches GI or lights.")
        opts.addWidget(self.chk_draft)
        opts.addStretch(1)
        lm.addLayout(opts)

        self.lock_list = QtWidgets.QListWidget()
        self.lock_list.setMaximumHeight(96)
        self.lock_list.setToolTip("Checked = locked. Locked parameters are never touched — "
                                  "not by the solver, not by the model.")
        lm.addWidget(self.lock_list)

        mrow = QtWidgets.QHBoxLayout()
        self.btn_match = QtWidgets.QPushButton("MATCH LIGHTING")
        self.btn_match.setObjectName("primary")
        self.btn_match.clicked.connect(self._start_match)
        mrow.addWidget(self.btn_match, 1)
        self.btn_match_all = QtWidgets.QPushButton("Match ALL (refs)")
        self.btn_match_all.setToolTip("Unattended queue: match every camera that has a "
                                      "reference bound, one after another.")
        self.btn_match_all.clicked.connect(self._start_match_all)
        mrow.addWidget(self.btn_match_all)
        self.btn_cancel = QtWidgets.QPushButton("Cancel")
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_match)
        mrow.addWidget(self.btn_cancel)
        lm.addLayout(mrow)

        self.log = QtWidgets.QTextEdit()      # rich text: iteration thumbnails render inline
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(150)
        lm.addWidget(self.log)
        lrow = QtWidgets.QHBoxLayout()
        btn_open_run = QtWidgets.QPushButton("Open run folder")
        btn_open_run.clicked.connect(self._open_run_dir)
        lrow.addWidget(btn_open_run)
        btn_restore = QtWidgets.QPushButton("Restore pre-match light")
        btn_restore.setToolTip("Put the lighting back exactly as it was before this "
                               "camera's last match run (snapshotted automatically).")
        btn_restore.clicked.connect(self._restore_pre_match)
        lrow.addWidget(btn_restore)
        self.btn_ab = QtWidgets.QPushButton("A/B")
        self.btn_ab.setToolTip("Flip between the pre-match light (A) and the matched "
                               "light (B) — Vantage mirrors the flip.")
        self.btn_ab.clicked.connect(self._ab_flip)
        lrow.addWidget(self.btn_ab)
        lrow.addStretch(1)
        lm.addLayout(lrow)
        right.addWidget(g_match)

        # refine group — talk to the gaffer
        g_ref2 = QtWidgets.QGroupBox("REFINE — TELL THE GAFFER")
        lref = QtWidgets.QVBoxLayout(g_ref2)
        lref.setSpacing(10)
        self.ed_note = QtWidgets.QLineEdit()
        self.ed_note.setPlaceholderText(
            "e.g. \"exposure is too much\" · \"sun should come more from the left\" · "
            "\"too warm, shadows too soft\"")
        self.ed_note.returnPressed.connect(self._start_refine)
        lref.addWidget(self.ed_note)
        chips = QtWidgets.QHBoxLayout()
        chips.setSpacing(6)
        for label in ("too bright", "too dark", "too warm", "too cool",
                      "harder shadows", "softer shadows", "sun more left",
                      "sun more right"):
            b = QtWidgets.QPushButton(label)
            b.setObjectName("chip")
            b.clicked.connect(lambda _c=False, t=label: self._chip_note(t))
            chips.addWidget(b)
        chips.addStretch(1)
        lref.addLayout(chips)
        rrow = QtWidgets.QHBoxLayout()
        self.btn_refine = QtWidgets.QPushButton("REFINE  ·  3-LENS ENSEMBLE")
        self.btn_refine.setObjectName("primary")
        self.btn_refine.setToolTip(
            "Your note takes instant effect via the craft table, then three agent lenses "
            "(exposure-first / geometry-first / mood-first) propose competing corrections — "
            "every branch is rendered and scored, the winner continues into a deep match "
            "with your note pinned into every prompt.")
        self.btn_refine.clicked.connect(self._start_refine)
        rrow.addWidget(self.btn_refine, 1)
        lref.addLayout(rrow)
        right.addWidget(g_ref2)

        # rig group (sliders built dynamically from the scene)
        g_rig = QtWidgets.QGroupBox("RIG — live controls")
        self.rig_form = QtWidgets.QFormLayout(g_rig)
        rig_btns = QtWidgets.QHBoxLayout()
        btn_read = QtWidgets.QPushButton("Read scene")
        btn_read.clicked.connect(self.rebuild_rig_controls)
        rig_btns.addWidget(btn_read)
        self.chk_live = QtWidgets.QCheckBox("live apply (Vantage mirrors)")
        self.chk_live.setChecked(True)
        rig_btns.addWidget(self.chk_live)
        btn_hdri = QtWidgets.QPushButton("HDRI…")
        btn_hdri.setToolTip("Swap the dome light's environment texture.")
        btn_hdri.clicked.connect(self._pick_hdri)
        rig_btns.addWidget(btn_hdri)
        btn_psave = QtWidgets.QPushButton("Save preset…")
        btn_psave.clicked.connect(self._save_preset)
        rig_btns.addWidget(btn_psave)
        btn_pload = QtWidgets.QPushButton("Load preset…")
        btn_pload.clicked.connect(self._load_preset)
        rig_btns.addWidget(btn_pload)
        self.rig_form.addRow(rig_btns)
        right.addWidget(g_rig)

        # vantage group
        g_v = QtWidgets.QGroupBox("VANTAGE + FINALS")
        lv = QtWidgets.QVBoxLayout(g_v)
        vrow = QtWidgets.QHBoxLayout()
        btn_link = QtWidgets.QPushButton("Live link (toggle)")
        btn_link.setToolTip("Runs V-Ray's 'Initiate a Live-Link to Chaos Vantage' action: "
                            "starts Vantage if needed, streams on port 20701. The SAME "
                            "action stops the link — it is a toggle.")
        btn_link.clicked.connect(self._start_live_link)
        vrow.addWidget(btn_link)
        self.lbl_link = QtWidgets.QLabel("link: unknown")
        self.lbl_link.setObjectName("dim")
        vrow.addWidget(self.lbl_link, 1)
        lv.addLayout(vrow)
        vrow2 = QtWidgets.QHBoxLayout()
        btn_render_sel = QtWidgets.QPushButton("Final render (selected)")
        btn_render_sel.setToolTip("Renders through V-Ray in Max at final resolution — "
                                  "stock Vantage 3.x has no headless CLI.")
        btn_render_sel.clicked.connect(lambda: self._render_finals(selected_only=True))
        vrow2.addWidget(btn_render_sel)
        btn_render_all = QtWidgets.QPushButton("Render ALL matched (V-Ray)")
        btn_render_all.clicked.connect(lambda: self._render_finals(selected_only=False))
        vrow2.addWidget(btn_render_all)
        lv.addLayout(vrow2)
        vrow3 = QtWidgets.QHBoxLayout()
        btn_export_v = QtWidgets.QPushButton("Export vrscenes → open Vantage")
        btn_export_v.setToolTip("Exports one .vrscene per matched camera (its light "
                                "applied) and opens Vantage — add them to Vantage's "
                                "in-app Batch Render queue for Vantage-quality finals.")
        btn_export_v.clicked.connect(self._export_for_vantage)
        vrow3.addWidget(btn_export_v)
        lv.addLayout(vrow3)
        right.addWidget(g_v)
        right.addStretch(1)

    # ================================================================= helpers
    def _log(self, msg: str):
        if msg.startswith("THUMB::"):
            url = QtCore.QUrl.fromLocalFile(msg[len("THUMB::"):]).toString()
            self.log.append(f'<img src="{url}" width="240">')
        else:
            self.log.append(_html.escape(msg))
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
        QtWidgets.QApplication.processEvents()

    def _run_blocking_io(self, fn):
        """Run pure-I/O ``fn`` on a worker while the MAIN thread spins a local event loop —
        Max stays alive, pymxs is never touched off-thread, exceptions re-raise here."""
        loop = QtCore.QEventLoop()
        box = {}
        w = _Worker(fn)
        self._workers.append(w)
        w.done.connect(lambda r: (box.__setitem__("r", r), loop.quit()))
        w.failed.connect(lambda e: (box.__setitem__("e", e), loop.quit()))
        w.start()
        loop.exec()
        w.wait()
        self._workers.remove(w)
        if "e" in box:
            raise RuntimeError(box["e"])
        return box.get("r")

    def _current_camera(self) -> str:
        item = self.cam_tree.currentItem()
        return item.text(0) if item else ""

    # ================================================================= cameras
    def refresh_cameras(self):
        current = self._current_camera()
        self.cam_tree.blockSignals(True)
        self.cam_tree.clear()
        try:
            cams = self.ctrl.cameras()
        except Exception as e:  # noqa: BLE001
            self._log(f"camera scan failed: {e}")
            cams = []
        for c in cams:
            score = f"{c['score']:.0f}" if c.get("score") is not None else ""
            item = QtWidgets.QTreeWidgetItem(
                [c["name"], "●" if c.get("reference") else "", score])
            if c.get("reference"):
                item.setForeground(1, QtGui.QBrush(QtGui.QColor(ACCENT)))
            self.cam_tree.addTopLevelItem(item)
            if c["name"] == current:
                self.cam_tree.setCurrentItem(item)
        self.cam_tree.blockSignals(False)
        self.chk_apply_on_select.setChecked(
            bool(self.ctrl.session.settings.get("apply_on_select", True)))
        if self.cam_tree.currentItem() is None and self.cam_tree.topLevelItemCount():
            self.cam_tree.setCurrentItem(self.cam_tree.topLevelItem(0))
        self.rebuild_rig_controls()

    def _on_camera_selected(self, item, _prev):
        if item is None:
            return
        if self._busy:   # a match/batch is mid-flight — applying a saved state now would
            self._log("busy — camera switch ignored until the current run finishes")
            return       # yank the rig out from under the loop
        self._ab_on_pre = False   # A/B toggle is per-camera-visit
        name = item.text(0)
        try:
            for w in self.ctrl.select_camera(name):
                self._log("⚠ " + w)
        except Exception as e:  # noqa: BLE001
            self._log(f"select failed: {e}")
        self._show_reference(name)
        self._rebuild_locks(name)
        self.rebuild_rig_controls()

    def _on_apply_on_select(self, checked: bool):
        self.ctrl.session.settings["apply_on_select"] = bool(checked)
        self.ctrl.save_session()

    # ================================================================= reference
    def _pick_reference(self):
        cam = self._current_camera()
        if not cam:
            self._log("select a camera first")
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, f"Reference for {cam}", "", "Images (*.jpg *.jpeg *.png *.webp)")
        if not path:
            return
        self.ctrl.session.set_reference(cam, path)
        if not self.ctrl.save_session():
            self._log("⚠ scene not saved yet — bindings live in memory only until you "
                      "save the .max file")
        self._show_reference(cam)
        self.refresh_cameras()

    def _show_reference(self, cam: str):
        e = self.ctrl.session.cameras.get(cam)
        ref = e.reference if e else ""
        if ref and os.path.exists(ref):
            pix = QtGui.QPixmap(ref)
            if not pix.isNull():
                self.ref_thumb.setPixmap(pix.scaled(
                    self.ref_thumb.size(), QtCore.Qt.KeepAspectRatio,
                    QtCore.Qt.SmoothTransformation))
                info = os.path.basename(ref)
                if e and e.semantics:
                    s = e.semantics
                    info += (f"\n{s.get('time_of_day')}, {s.get('sky')} sky, "
                             f"wb ~{s.get('wb_kelvin_estimate', 0):.0f}K")
                if e and e.score is not None:
                    info += f"\nlast match: {e.score:.1f}/100 at {e.matched_at}"
                self.lbl_ref_info.setText(info)
                return
        self.ref_thumb.setPixmap(QtGui.QPixmap())
        self.ref_thumb.setText("no reference")
        self.lbl_ref_info.setText("Bind a lighting reference image to this camera.")

    def _rebuild_locks(self, cam: str):
        self.lock_list.clear()
        e = self.ctrl.session.cameras.get(cam)
        locked = set(e.locks) if e else set()
        try:
            state = self.ctrl.read_state(cam)
        except Exception:
            state = LightingState()
        for key in sorted(state.keys()):
            it = QtWidgets.QListWidgetItem(key)
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Checked if key in locked else QtCore.Qt.Unchecked)
            self.lock_list.addItem(it)

    def _locks(self) -> set:
        out = set()
        for i in range(self.lock_list.count()):
            it = self.lock_list.item(i)
            if it.checkState() == QtCore.Qt.Checked:
                out.add(it.text())
        return out

    # ================================================================= rig sliders
    def rebuild_rig_controls(self):
        while self.rig_form.rowCount() > 1:      # row 0 = the buttons row
            self.rig_form.removeRow(1)
        self._sliders.clear()
        try:
            state = self.ctrl.read_state(self._current_camera())
        except Exception as e:  # noqa: BLE001
            self.rig_form.addRow(QtWidgets.QLabel(f"rig read failed: {e}"))
            return
        for key in sorted(state.keys()):
            spec = spec_for(key)
            if spec is None:
                continue
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(spec.lo, spec.hi)
            spin.setDecimals(2)
            spin.setSingleStep(1.0 if spec.hi - spec.lo > 20 else 0.1)
            spin.setValue(state.get(key))
            spin.valueChanged.connect(lambda v, k=key: self._on_slider(k, v))
            self._sliders[key] = spin
            self.rig_form.addRow(key, spin)

    def _pick_hdri(self):
        if self._busy:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Dome HDRI", "", "HDR images (*.hdr *.exr *.jpg *.png *.tif)")
        if not path:
            return
        how = self.ctrl.set_dome_hdri(path)
        self._log(f"dome HDRI → {os.path.basename(path)} ({how})" if how != "failed"
                  else "✗ could not set the dome texture (no dome, or unknown file prop — "
                       "checklist #16)")

    def _save_preset(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save lighting preset", "", "MaxGaffer preset (*.json)")
        if not path:
            return
        ok = self.ctrl.save_preset(path, self._current_camera())
        self._log(f"preset saved → {path}" if ok else f"✗ could not write {path}")

    def _load_preset(self):
        if self._busy:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load lighting preset", "", "MaxGaffer preset (*.json)")
        if not path:
            return
        try:
            for w in self.ctrl.load_preset(path, self._current_camera()):
                self._log("⚠ " + w)
            self._log(f"preset applied: {os.path.basename(path)}")
            self.rebuild_rig_controls()
            self.refresh_cameras()
        except Exception as err:  # noqa: BLE001
            self._log(f"✗ {err}")

    def _on_slider(self, key: str, value: float):
        if not self.chk_live.isChecked() or self._busy:
            return
        st = LightingState()
        if key.startswith(GROUP_PREFIX):
            st.groups[key[len(GROUP_PREFIX):]] = value
        else:
            st.set(key, value)
        try:
            self.ctrl.apply_state(st, self._current_camera())
        except Exception as e:  # noqa: BLE001
            self._log(f"apply failed: {e}")

    # ================================================================= match
    def _start_match(self):
        if self._busy:
            return
        cam = self._current_camera()
        if not cam:
            self._log("select a camera first")
            return
        e = self.ctrl.session.cameras.get(cam)
        if not (e and e.reference):
            self._log("bind a reference image first")
            return
        if not self.cfg.api_key:
            self._log("no API key — open Settings and paste your oc_ key")
            return
        self._busy = True
        self._cancel = False
        self.btn_match.setEnabled(False)
        self.btn_match_all.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.cfg.max_iterations = int(self.spin_iters.value())
        self.cfg.target_score = float(self.spin_target.value())
        self.cfg.draft_sampler = self.chk_draft.isChecked()
        self.cfg.plan_first = self.chk_plan.isChecked()
        self.cfg.auto_execute_plan = self.chk_auto_exec.isChecked()
        self.log.clear()
        self._log(f"— match: {cam} —")
        plan_report = None
        try:
            # everything scene-touching stays on the MAIN thread; gateway calls come back
            # through ctrl.io → _run_blocking_io, so the UI breathes and Cancel works.
            if self.chk_plan.isChecked():
                ops, lines, meta, _raw = self.ctrl.make_plan(cam, log=self._log)
                if not ops:
                    self._log("plan: no operations proposed — continuing to the match loop")
                elif self.chk_auto_exec.isChecked() or PlanPreviewDialog(
                        lines, meta, self).exec():
                    self._log(f"— executing plan ({len(ops)} ops) —")
                    plan_report = self.ctrl.execute_plan(ops, cam, log=self._log)
                    if meta.get("expects"):
                        self._log("expected: " + meta["expects"])
                else:
                    self._log("plan declined — continuing with the match loop only")
            result = self.ctrl.run_match(
                cam, log=self._log,
                should_cancel=lambda: self._cancel,
                locks=self._locks(),
                do_sweep=self.chk_sweep.isChecked(),
                deep=self.chk_deep.isChecked())
            score = f"{result.best_score:.1f}" if result.best_score is not None else "n/a"
            ceiling = (" · ceiling proven — the gap left is content, not lighting"
                       if result.ceiling_converged and (result.best_score or 0) < 99 else "")
            self._log(f"✓ done ({result.stop_reason}) — best {score}{ceiling}")
            self._set_match_thumb(result.best_render)
            if self.cfg.show_report_popup:
                ChangeReportDialog(plan_report,
                                   self.ctrl.state_change_rows(cam),
                                   f"{cam} — {result.stop_reason}, score {score}",
                                   self).exec()
        except (OmegaError, RuntimeError) as err:
            self._log(f"✗ {err}")
        except Exception as err:  # noqa: BLE001
            self._log(f"✗ unexpected: {err}")
        finally:
            self._busy = False
            self._ab_on_pre = False        # a fresh match lands on B (matched)
            self.btn_match.setEnabled(True)
            self.btn_match_all.setEnabled(True)
            self.btn_cancel.setEnabled(False)
            self.refresh_cameras()
            self.rebuild_rig_controls()
            self._show_reference(cam)

    def _start_match_all(self):
        if self._busy:
            return
        queue = [n for n, e in self.ctrl.session.cameras.items() if e.reference]
        if not queue:
            self._log("no cameras have references bound — bind references first")
            return
        est = len(queue) * (int(self.spin_iters.value())
                            + (self.cfg.sweep_count if self.chk_sweep.isChecked() else 0))
        if QtWidgets.QMessageBox.question(
                self, "Match ALL",
                f"Match {len(queue)} camera(s) sequentially (~{est} loop renders total)?\n"
                f"{', '.join(queue)}",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        ) != QtWidgets.QMessageBox.Yes:
            return
        self._busy = True
        self._cancel = False
        self.btn_match.setEnabled(False)
        self.btn_match_all.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.cfg.max_iterations = int(self.spin_iters.value())
        self.cfg.target_score = float(self.spin_target.value())
        self.cfg.draft_sampler = self.chk_draft.isChecked()
        self.log.clear()
        self._log(f"— batch match: {len(queue)} cameras —")
        try:
            results = self.ctrl.match_all(log=self._log,
                                          should_cancel=lambda: self._cancel,
                                          do_sweep=self.chk_sweep.isChecked())
            self._log("— batch summary —")
            for cam, status in results.items():
                self._log(f"  {cam}: {status}")
        except Exception as err:  # noqa: BLE001
            self._log(f"✗ batch: {err}")
        finally:
            self._busy = False
            self._ab_on_pre = False
            self.btn_match.setEnabled(True)
            self.btn_match_all.setEnabled(True)
            self.btn_cancel.setEnabled(False)
            self.refresh_cameras()
            self.rebuild_rig_controls()

    def _cancel_match(self):
        self._cancel = True
        self._log("cancelling after the current step…")

    def _chip_note(self, text: str):
        cur = self.ed_note.text().strip()
        self.ed_note.setText((cur + ", " + text) if cur else text)

    def _set_match_thumb(self, path):
        if path and os.path.exists(path):
            pix = QtGui.QPixmap(path)
            if not pix.isNull():
                self.match_thumb.setPixmap(pix.scaled(
                    self.match_thumb.size(), QtCore.Qt.KeepAspectRatio,
                    QtCore.Qt.SmoothTransformation))
                return
        self.match_thumb.setPixmap(QtGui.QPixmap())
        self.match_thumb.setText("no match yet")

    def _start_refine(self):
        if self._busy:
            return
        cam = self._current_camera()
        note = self.ed_note.text().strip()
        if not cam or not note:
            self._log("select a camera and type a note first")
            return
        e = self.ctrl.session.cameras.get(cam)
        if not (e and e.reference):
            self._log("bind a reference image first")
            return
        self._busy = True
        self._cancel = False
        self.btn_match.setEnabled(False)
        self.btn_match_all.setEnabled(False)
        self.btn_refine.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self._log(f"— refine: {cam} — “{note}”")
        try:
            result = self.ctrl.refine(cam, note, log=self._log,
                                      should_cancel=lambda: self._cancel)
            score = f"{result.best_score:.1f}" if result.best_score is not None else "n/a"
            ceiling = (" · ceiling proven — the gap left is content, not lighting"
                       if result.ceiling_converged and (result.best_score or 0) < 99 else "")
            self._log(f"✓ refine done ({result.stop_reason}) — best {score}{ceiling}")
            self._set_match_thumb(result.best_render)
            self.ed_note.clear()
            if self.cfg.show_report_popup:
                ChangeReportDialog(None, self.ctrl.state_change_rows(cam),
                                   f"{cam} — refined to {score}", self).exec()
        except (OmegaError, RuntimeError) as err:
            self._log(f"✗ {err}")
        except Exception as err:  # noqa: BLE001
            self._log(f"✗ unexpected: {err}")
        finally:
            self._busy = False
            self._ab_on_pre = False
            self.btn_match.setEnabled(True)
            self.btn_match_all.setEnabled(True)
            self.btn_refine.setEnabled(True)
            self.btn_cancel.setEnabled(False)
            self.refresh_cameras()
            self.rebuild_rig_controls()
            self._show_reference(cam)

    def _open_run_dir(self):
        d = self.ctrl._run_dir or cfgmod.sessions_dir()
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(d))

    def _restore_pre_match(self):
        if self._busy:
            return
        cam = self._current_camera()
        if cam and self.ctrl.restore_pre_match(cam):
            self._log(f"restored pre-match lighting for {cam}")
            self._ab_on_pre = True
            self.rebuild_rig_controls()
        else:
            self._log("no pre-match snapshot for this camera yet")

    def _ab_flip(self):
        if self._busy:
            return
        cam = self._current_camera()
        e = self.ctrl.session.cameras.get(cam) if cam else None
        if not (e and e.pre_match is not None and e.state is not None):
            self._log("A/B needs both a pre-match snapshot and a matched state — run a "
                      "match first")
            return
        self._ab_on_pre = not getattr(self, "_ab_on_pre", False)
        try:
            self.ctrl.apply_state(e.pre_match if self._ab_on_pre else e.state, cam)
            self._log(f"A/B → showing {'A (pre-match)' if self._ab_on_pre else 'B (matched)'}")
            self.rebuild_rig_controls()
        except Exception as err:  # noqa: BLE001
            self._log(f"A/B failed: {err}")

    # ================================================================= vantage
    def _start_live_link(self):
        ok, how = self.ctrl.start_live_link()
        self.lbl_link.setText(("link: started — " if ok else "link: ") + how)
        self._log(("vantage live link: " if ok else "⚠ vantage live link: ") + how)

    def _final_targets(self, selected_only: bool):
        cams = ([self._current_camera()] if selected_only
                else self.ctrl.session.cameras_with_states())
        return [c for c in cams if c]

    def _render_finals(self, selected_only: bool):
        if self._busy:
            return
        cams = self._final_targets(selected_only)
        if not cams:
            self._log("no cameras to render (match or save states first)")
            return
        out_dir = QtWidgets.QFileDialog.getExistingDirectory(self, "Output folder")
        if not out_dir:
            return
        self._busy = True
        try:
            if self.cfg.final_render_backend == "vantage_cli":
                # Developer-Edition CLI only — exports main-thread, renders on a worker
                jobs = self.ctrl.prepare_vantage_jobs(
                    cams, out_dir, on_progress=lambda c, s: self._log(f"vantage {c}: {s}"))
                relay = _ProgressRelay()
                relay.progress.connect(lambda c, s: self._log(f"vantage {c}: {s}"))
                results = self._run_blocking_io(
                    lambda: self.ctrl.run_vantage_jobs(
                        jobs, on_progress=lambda c, s: relay.progress.emit(c, s)))
            else:
                results = self.ctrl.render_finals_vray(
                    cams, out_dir, on_progress=lambda c, s: self._log(f"final {c}: {s}"))
            for cam, status in results.items():
                self._log(f"{'✓' if status == 'ok' else '✗'} {cam}: {status}")
        except Exception as e:  # noqa: BLE001
            self._log(f"✗ final renders: {e}")
        finally:
            self._busy = False

    def _export_for_vantage(self):
        if self._busy:
            return
        cams = self._final_targets(selected_only=False)
        if not cams:
            self._log("no matched cameras to export")
            return
        self._busy = True
        try:
            jobs, launched, export_dir = self.ctrl.export_and_open_vantage(
                cams, on_progress=lambda c, s: self._log(f"export {c}: {s}"))
            self._log(f"✓ {len(jobs)} vrscene(s) → {export_dir}")
            self._log("Vantage opened — add the files to its Batch Render queue"
                      if launched else
                      f"⚠ could not launch Vantage ({self.cfg.vantage_exe}) — open the "
                      "folder manually")
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(export_dir))
        except Exception as e:  # noqa: BLE001
            self._log(f"✗ vantage export: {e}")
        finally:
            self._busy = False

    # ================================================================= settings
    def _open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            self.cfg.save()
            self.ctrl.cfg = self.cfg
            self._log("settings saved")


class PlanPreviewDialog(QtWidgets.QDialog):
    """The approval gate: the model's scene read + every operation it wants to run."""

    def __init__(self, lines, meta, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MaxGaffer — change plan")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(560)
        lay = QtWidgets.QVBoxLayout(self)
        read = QtWidgets.QLabel(meta.get("read") or "")
        read.setWordWrap(True)
        read.setObjectName("dim")
        lay.addWidget(read)
        box = QtWidgets.QPlainTextEdit("\n".join(lines))
        box.setReadOnly(True)
        box.setMinimumHeight(220)
        lay.addWidget(box)
        note = QtWidgets.QLabel("Executes as ONE undo step · new lights land on the "
                                "MG_lights layer.")
        note.setObjectName("dim")
        lay.addWidget(note)
        row = QtWidgets.QHBoxLayout()
        ok = QtWidgets.QPushButton(f"EXECUTE {len(lines)} OPS")
        ok.setObjectName("primary")
        ok.clicked.connect(self.accept)
        row.addWidget(ok, 1)
        skip = QtWidgets.QPushButton("Skip plan")
        skip.clicked.connect(self.reject)
        row.addWidget(skip)
        lay.addLayout(row)


class ChangeReportDialog(QtWidgets.QDialog):
    """The 'scene changed' popup: values changed (before → after), lights placed, warnings."""

    def __init__(self, plan_report, state_rows, headline, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MaxGaffer — scene changed")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(600)
        lay = QtWidgets.QVBoxLayout(self)
        head = QtWidgets.QLabel(headline)
        head.setStyleSheet(f"color:{ACCENT};font-weight:600;letter-spacing:1px;")
        lay.addWidget(head)
        tree = QtWidgets.QTreeWidget()
        tree.setHeaderLabels(["what", "before", "after", "why"])
        tree.setRootIsDecorated(True)
        tree.setColumnWidth(0, 240)

        def add_group(title, rows, fmt):
            if not rows:
                return
            top = QtWidgets.QTreeWidgetItem([f"{title} ({len(rows)})", "", "", ""])
            top.setForeground(0, QtGui.QBrush(QtGui.QColor(ACCENT)))
            tree.addTopLevelItem(top)
            for r in rows:
                top.addChild(QtWidgets.QTreeWidgetItem(fmt(r)))
            top.setExpanded(True)

        pr = plan_report or {"changes": [], "created": [], "warnings": []}
        if pr.get("effect"):
            eff = pr["effect"]
            worse = eff["after"] < eff["before"] - 5.0
            eff_lbl = QtWidgets.QLabel(
                f"plan effect (measured): critic {eff['before']:.1f} → {eff['after']:.1f}"
                + ("   ⚠ worse — one Ctrl+Z reverts the plan" if worse else ""))
            eff_lbl.setStyleSheet(f"color:{ERR};" if worse else f"color:{OK};")
            lay.addWidget(eff_lbl)
        add_group("Plan — values changed", pr["changes"], lambda c: [
            f"{c['target']} · {c['prop']}", str(c["before"]), str(c["after"]),
            c.get("why", "")])
        add_group("Plan — lights placed", pr["created"], lambda c: [
            f"{c['type']}  '{c['name']}'", "", c["at"], c.get("why", "")])
        add_group("Match loop — lighting values", state_rows, lambda c: [
            c["prop"], str(c["before"]), str(c["after"]), ""])
        add_group("Warnings", [{"w": w} for w in pr["warnings"]],
                  lambda c: [c["w"], "", "", ""])
        if tree.topLevelItemCount() == 0:
            tree.addTopLevelItem(QtWidgets.QTreeWidgetItem(
                ["no changes were applied", "", "", ""]))
        lay.addWidget(tree)
        note = QtWidgets.QLabel("Plan = one Ctrl+Z · match loop states restorable via "
                                "'Restore pre-match light'.")
        note.setObjectName("dim")
        lay.addWidget(note)
        ok = QtWidgets.QPushButton("OK")
        ok.setObjectName("primary")
        ok.clicked.connect(self.accept)
        lay.addWidget(ok)


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, cfg: cfgmod.Config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MaxGaffer — settings")
        self.setStyleSheet(STYLE)
        self.cfg = cfg
        form = QtWidgets.QFormLayout(self)
        self.ed_key = QtWidgets.QLineEdit(cfg.api_key)
        self.ed_key.setEchoMode(QtWidgets.QLineEdit.Password)
        form.addRow("oc_ API key", self.ed_key)
        self.ed_model = QtWidgets.QLineEdit(cfg.model)
        form.addRow("model", self.ed_model)
        self.ed_vantage = QtWidgets.QLineEdit(cfg.vantage_console)
        form.addRow("vantage_console.exe", self.ed_vantage)
        self.ed_syspy = QtWidgets.QLineEdit(cfg.system_python)
        self.ed_syspy.setPlaceholderText("optional: python.exe with Pillow (sidecar)")
        form.addRow("system python", self.ed_syspy)
        res = QtWidgets.QHBoxLayout()
        self.sp_w = QtWidgets.QSpinBox()
        self.sp_w.setRange(160, 1920)
        self.sp_w.setValue(cfg.loop_width)
        self.sp_h = QtWidgets.QSpinBox()
        self.sp_h.setRange(90, 1080)
        self.sp_h.setValue(cfg.loop_height)
        res.addWidget(self.sp_w)
        res.addWidget(QtWidgets.QLabel("×"))
        res.addWidget(self.sp_h)
        form.addRow("loop render size", res)
        self.lbl_status = QtWidgets.QLabel("")
        self.lbl_status.setObjectName("dim")
        form.addRow(self.lbl_status)
        btns = QtWidgets.QHBoxLayout()
        b_test = QtWidgets.QPushButton("Test gateway")
        b_test.clicked.connect(self._test)
        btns.addWidget(b_test)
        b_ok = QtWidgets.QPushButton("Save")
        b_ok.setObjectName("primary")
        b_ok.clicked.connect(self._save)
        btns.addWidget(b_ok)
        b_cancel = QtWidgets.QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        btns.addWidget(b_cancel)
        form.addRow(btns)

    def _test(self):
        self.lbl_status.setText("pinging…")
        QtWidgets.QApplication.processEvents()
        try:
            self.lbl_status.setText(ping(self.ed_key.text().strip(),
                                         self.ed_model.text().strip()))
            self.lbl_status.setStyleSheet(f"color:{OK};")
        except OmegaError as e:
            self.lbl_status.setText(str(e))
            self.lbl_status.setStyleSheet(f"color:{ERR};")

    def _save(self):
        self.cfg.api_key = self.ed_key.text().strip()
        self.cfg.model = self.ed_model.text().strip() or "claude-opus-4-8"
        self.cfg.vantage_console = self.ed_vantage.text().strip()
        self.cfg.system_python = self.ed_syspy.text().strip()
        self.cfg.loop_width = int(self.sp_w.value())
        self.cfg.loop_height = int(self.sp_h.value())
        self.accept()


_dock_instance: Optional[MaxGafferDock] = None


def show_dock():
    """Create (or raise) the dock inside 3ds Max's main window."""
    global _dock_instance
    parent = None
    try:
        import qtmax  # Max 2021+

        parent = qtmax.GetQMaxMainWindow()
    except Exception:
        parent = None
    if _dock_instance is not None:
        try:
            _dock_instance.show()
            _dock_instance.raise_()
            return _dock_instance
        except RuntimeError:
            _dock_instance = None
    if parent is not None:
        dock = QtWidgets.QDockWidget("MaxGaffer", parent)
        dock.setObjectName("MaxGafferDock")
        widget = MaxGafferDock(dock)
        dock.setWidget(widget)
        parent.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)
        dock.setFloating(True)
        dock.resize(940, 1080)
        dock.show()
        _dock_instance = widget
    else:  # dev fallback: plain window
        _dock_instance = MaxGafferDock()
        _dock_instance.resize(940, 1080)
        _dock_instance.show()
    return _dock_instance
