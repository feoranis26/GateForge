from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
import sys

from PySide6.QtCore import QEvent, QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QFont, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyle,
    QTabWidget,
    QTableView,
    QToolBar,
    QToolButton,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gateforge.visualization.model import VisualDocument, VisualElement, VisualView
from gateforge.workbench.protocol import JsonValue, WorkerCommand, WorkerEvent
from gateforge.workbench.qt_canvas import VisualSceneView, YosysSvgView
from gateforge.workbench.qt_models import (
    SOURCE_CANDIDATE_ROLE,
    ClaimsTableModel,
    CompilationCandidateTreeModel,
    ProposalTableModel,
)
from gateforge.workbench.worker import WorkerClient


class _TaskSignals(QObject):
    completed = Signal(object)
    failed = Signal(object)


class _WorkerTask(QRunnable):
    def __init__(self, operation: Callable[[], object]) -> None:
        super().__init__()
        self.operation = operation
        self.signals = _TaskSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.operation()
        except Exception as error:
            self.signals.failed.emit(error)
        else:
            self.signals.completed.emit(result)


class WorkbenchWindow(QMainWindow):
    snapshot_changed = Signal(object)
    operation_finished = Signal(str)

    def __init__(
        self,
        source: str | Path | None = None,
        *,
        worker: WorkerClient | None = None,
    ) -> None:
        super().__init__()
        self.worker = worker or WorkerClient()
        self.source_path: Path | None = None
        self.snapshot: dict[str, object] | None = None
        self._document: VisualDocument | None = None
        self._busy = False
        self._continuing = False
        self._generation = 0
        self._tasks: set[_WorkerTask] = set()
        self._visual_tabs: list[QWidget] = []
        self._scene_tabs: dict[str, tuple[QTabWidget, int]] = {}
        self._scene_canvases: list[VisualSceneView] = []
        self._inspector_canvas: VisualSceneView | None = None
        self._candidate_model = CompilationCandidateTreeModel()
        self._proposal_model = ProposalTableModel()
        self._claims_model = ClaimsTableModel()

        self.setWindowTitle("GateForge Workbench")
        self.resize(1560, 940)
        self.setMinimumSize(1080, 680)
        self._build_actions()
        self._build_ui()
        self._apply_style()
        self._update_actions()
        if source is not None:
            QTimer.singleShot(0, lambda: self.open_source(source))

    def _build_actions(self) -> None:
        style = self.style()
        self.action_open = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton),
            "Open",
            self,
        )
        self.action_open.setObjectName("actionOpen")
        self.action_open.setShortcut(QKeySequence.StandardKey.Open)
        self.action_open.triggered.connect(self._choose_source)

        self.action_reload = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload),
            "Reload",
            self,
        )
        self.action_reload.setObjectName("actionReload")
        self.action_reload.setShortcut(QKeySequence.StandardKey.Refresh)
        self.action_reload.triggered.connect(self.reload_source)

        self.action_next = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaSkipForward),
            "Next Stage",
            self,
        )
        self.action_next.setObjectName("actionNextStage")
        self.action_next.setShortcut(QKeySequence("F10"))
        self.action_next.triggered.connect(self.next_stage)

        self.action_continue = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay),
            "Continue",
            self,
        )
        self.action_continue.setObjectName("actionContinue")
        self.action_continue.setShortcut(QKeySequence("F5"))
        self.action_continue.triggered.connect(self.continue_search)

        self.action_stop = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaStop),
            "Stop",
            self,
        )
        self.action_stop.setObjectName("actionStop")
        self.action_stop.setShortcut(QKeySequence("Shift+F5"))
        self.action_stop.triggered.connect(self.stop)

        self.action_compile = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_CommandLink),
            "Compile Material",
            self,
        )
        self.action_compile.setObjectName("actionCompileMaterial")
        self.action_compile.setShortcut(QKeySequence("Ctrl+B"))
        self.action_compile.triggered.connect(self.compile_material)

        self.action_place = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_ArrowRight),
            "Place",
            self,
        )
        self.action_place.setObjectName("actionPlace")
        self.action_place.triggered.connect(self.place)

        self.action_fit = QAction("Fit", self)
        self.action_fit.setObjectName("actionFit")
        self.action_fit.setShortcut(QKeySequence("F"))
        self.action_fit.triggered.connect(self.fit_active_view)

        self.action_wires = _toggle_action("Wires", "actionWires", True, self)
        self.action_labels = _toggle_action("Labels", "actionLabels", True, self)
        self.action_bounds = _toggle_action("Bounds", "actionBounds", False, self)
        self.action_collisions = _toggle_action(
            "Collisions", "actionCollisions", True, self
        )
        for action in (
            self.action_wires,
            self.action_labels,
            self.action_bounds,
            self.action_collisions,
        ):
            action.triggered.connect(self._apply_overlays)

        self.save_actions: dict[str, QAction] = {}
        for kind, label in (
            ("material", "Material"),
            ("state", "State"),
            ("report", "Search Report"),
            ("placement", "Placement"),
            ("visualization", "Visual Document"),
        ):
            action = QAction(label, self)
            action.setObjectName(f"actionSave{kind.title()}")
            action.triggered.connect(
                lambda _checked=False, selected=kind: self.save_artifact(selected)
            )
            self.save_actions[kind] = action

        self.action_export = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton),
            "Export LBP",
            self,
        )
        self.action_export.setObjectName("actionExportLbp")
        self.action_export.triggered.connect(self.export_lbp)

    def _build_ui(self) -> None:
        toolbar = QToolBar("Compiler", self)
        toolbar.setObjectName("compilerToolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)
        for action in (
            self.action_open,
            self.action_reload,
            self.action_next,
            self.action_continue,
            self.action_stop,
            self.action_compile,
            self.action_place,
        ):
            toolbar.addAction(action)

        self.addToolBarBreak()
        placement_toolbar = QToolBar("Placement", self)
        placement_toolbar.setObjectName("placementToolbar")
        placement_toolbar.setMovable(False)
        self.addToolBar(placement_toolbar)

        self.physical_hierarchy_combo = QComboBox()
        self.physical_hierarchy_combo.setObjectName("physicalHierarchyCombo")
        self.physical_hierarchy_combo.addItems(
            ("flat", "preserve-all", "min-objects", "min-cost")
        )
        self.physical_hierarchy_combo.setToolTip("HDL hierarchy retained by placement")

        self.hierarchy_threshold_spin = QDoubleSpinBox()
        self.hierarchy_threshold_spin.setObjectName("hierarchyThresholdSpin")
        self.hierarchy_threshold_spin.setRange(0.1, 1_000_000_000.0)
        self.hierarchy_threshold_spin.setDecimals(1)
        self.hierarchy_threshold_spin.setValue(3.0)
        self.hierarchy_threshold_spin.setToolTip("Retention threshold for minimum modes")

        self.generated_hierarchy_combo = QComboBox()
        self.generated_hierarchy_combo.setObjectName("generatedHierarchyCombo")
        self.generated_hierarchy_combo.addItems(("inline", "auto", "all"))
        self.generated_hierarchy_combo.setCurrentText("auto")
        self.generated_hierarchy_combo.setToolTip(
            "Generated implementation hierarchy selected by placement"
        )

        self.column_pitch_spin = _placement_number_control(
            "columnPitchSpin",
            210.0,
            52.5,
            "Horizontal topological column pitch",
        )
        self.row_pitch_spin = _placement_number_control(
            "rowPitchSpin",
            52.5,
            26.25,
            "Minimum vertical subject pitch",
        )
        self.routing_group_height_spin = _placement_number_control(
            "routingGroupHeightSpin",
            250.0,
            50.0,
            "Content height between routing gaps",
        )
        self.routing_gap_rows_spin = QSpinBox()
        self.routing_gap_rows_spin.setObjectName("routingGapRowsSpin")
        self.routing_gap_rows_spin.setRange(0, 100)
        self.routing_gap_rows_spin.setValue(1)
        self.routing_gap_rows_spin.setToolTip("Empty rows reserved per routing gap")

        for label, control in (
            ("HDL", self.physical_hierarchy_combo),
            ("Threshold", self.hierarchy_threshold_spin),
            ("Generated", self.generated_hierarchy_combo),
            ("Column", self.column_pitch_spin),
            ("Row", self.row_pitch_spin),
            ("Route band", self.routing_group_height_spin),
            ("Gap rows", self.routing_gap_rows_spin),
        ):
            placement_toolbar.addWidget(_toolbar_label(label))
            placement_toolbar.addWidget(control)
        self._placement_controls = (
            self.physical_hierarchy_combo,
            self.hierarchy_threshold_spin,
            self.generated_hierarchy_combo,
            self.column_pitch_spin,
            self.row_pitch_spin,
            self.routing_group_height_spin,
            self.routing_gap_rows_spin,
        )
        self.physical_hierarchy_combo.currentTextChanged.connect(
            lambda _text: self._update_actions()
        )
        toolbar.addSeparator()
        save_button = QToolButton()
        save_button.setObjectName("saveArtifactsButton")
        save_button.setText("Save")
        save_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton)
        )
        save_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        save_menu = QMenu(save_button)
        for action in self.save_actions.values():
            save_menu.addAction(action)
        save_button.setMenu(save_menu)
        toolbar.addWidget(save_button)
        toolbar.addAction(self.action_export)
        toolbar.addSeparator()
        toolbar.addAction(self.action_fit)
        for action in (
            self.action_wires,
            self.action_labels,
            self.action_bounds,
            self.action_collisions,
        ):
            toolbar.addAction(action)

        root = QSplitter(Qt.Orientation.Horizontal)
        root.setChildrenCollapsible(False)
        self.setCentralWidget(root)

        left = QSplitter(Qt.Orientation.Vertical)
        left.setChildrenCollapsible(False)
        source_frame = QFrame()
        source_layout = QVBoxLayout(source_frame)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(0)
        source_layout.addWidget(_panel_label("SOURCE"))
        self.source_editor = QPlainTextEdit()
        self.source_editor.setObjectName("sourceEditor")
        self.source_editor.setReadOnly(True)
        self.source_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.source_editor.setFont(QFont("JetBrains Mono", 10))
        source_layout.addWidget(self.source_editor)
        left.addWidget(source_frame)

        pipeline_frame = QFrame()
        pipeline_layout = QVBoxLayout(pipeline_frame)
        pipeline_layout.setContentsMargins(0, 0, 0, 0)
        pipeline_layout.setSpacing(0)
        pipeline_layout.addWidget(_panel_label("PIPELINE"))
        self.pipeline = QTreeWidget()
        self.pipeline.setObjectName("pipelineTree")
        self.pipeline.setHeaderLabels(("Stage", "Status", "Branches"))
        self.pipeline.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.pipeline.currentItemChanged.connect(self._pipeline_selected)
        pipeline_layout.addWidget(self.pipeline)
        left.addWidget(pipeline_frame)
        left.setSizes((560, 260))
        root.addWidget(left)

        self.content_tabs = QTabWidget()
        self.content_tabs.setObjectName("contentTabs")
        self.yosys_view = YosysSvgView()
        self.yosys_view.setObjectName("yosysView")
        self.content_tabs.addTab(self.yosys_view, "Yosys")
        self.material_summary = QTreeWidget()
        self.material_summary.setObjectName("materialSummary")
        self.material_summary.setHeaderLabels(("Artifact", "Value"))
        self.material_summary.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.material_summary.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.content_tabs.addTab(self.material_summary, "Material")
        root.addWidget(self.content_tabs)

        inspector_tabs = QTabWidget()
        inspector_tabs.setObjectName("inspectorTabs")
        self.candidates = QTreeView()
        self.candidates.setObjectName("candidateTree")
        self.candidates.setModel(self._candidate_model)
        self.candidates.setAlternatingRowColors(True)
        self.candidates.setSelectionBehavior(QTreeView.SelectionBehavior.SelectRows)
        self.candidates.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.candidates.selectionModel().currentChanged.connect(
            self._candidate_selected
        )
        inspector_tabs.addTab(self.candidates, "Branches")

        self.proposals = _table(self._proposal_model, "proposalTable")
        inspector_tabs.addTab(self.proposals, "Proposals")
        self.claims = _table(self._claims_model, "claimsTable")
        inspector_tabs.addTab(self.claims, "Claims")
        self.inspector = QTreeWidget()
        self.inspector.setObjectName("propertyInspector")
        self.inspector.setHeaderLabels(("Property", "Value"))
        self.inspector.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.inspector.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.inspector.currentItemChanged.connect(self._inspector_selected)
        inspector_tabs.addTab(self.inspector, "Inspector")
        root.addWidget(inspector_tabs)
        root.setSizes((360, 820, 420))
        root.setStretchFactor(1, 1)

        self.phase_label = QLabel("No source")
        self.phase_label.setObjectName("phaseLabel")
        self.statusBar().addPermanentWidget(self.phase_label)
        self.statusBar().showMessage("Ready")

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QSplitter, QTabWidget::pane { background: #f4f3ef; }
            QToolBar { background: #233833; border: 0; spacing: 5px; padding: 5px; }
            QToolBar QToolButton { color: #f5f2e9; padding: 5px 7px; }
            QToolBar QToolButton:hover { background: #35544b; }
            QToolBar QLabel#toolbarLabel { color: #dfe8e3; padding: 0 3px 0 8px; }
            QToolBar QComboBox, QToolBar QDoubleSpinBox, QToolBar QSpinBox {
                background: #fbfaf7; color: #202724; border: 1px solid #779087;
                min-height: 24px; padding: 1px 4px;
            }
            QTabBar::tab { padding: 7px 12px; background: #deddd7; color: #293430; }
            QTabBar::tab:selected { background: #f4f3ef; border-top: 3px solid #d28f2c; }
            QPlainTextEdit, QTreeView, QTreeWidget, QTableView {
                background: #fbfaf7; color: #202724; border: 0; gridline-color: #d7d5cf;
                selection-background-color: #c8dfd8; selection-color: #17211d;
            }
            QHeaderView::section { background: #e7e5df; color: #33443e; border: 0;
                border-right: 1px solid #cccac4; padding: 5px; }
            QLabel#panelLabel { background: #334b43; color: #f5f2e9; padding: 6px 9px;
                font-family: "Noto Sans"; font-size: 11px; font-weight: 700; }
            QStatusBar { background: #233833; color: #f5f2e9; }
            QLabel#phaseLabel { color: #efbd59; padding: 0 8px; }
            """
        )

    def open_source(self, source: str | Path) -> None:
        path = Path(source).resolve()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            self.statusBar().showMessage(f"Open failed: {error}")
            return
        self.stop()
        self.source_path = path
        self.source_editor.setPlainText(text)
        self.setWindowTitle(f"{path.name} - GateForge Workbench")
        self.snapshot = None
        self._clear_session_views()
        self._run_worker(
            "Opening source",
            lambda: self.worker.open_session(path),
            self._accept_snapshot_event,
        )

    def reload_source(self) -> None:
        if self.source_path is not None:
            self.open_source(self.source_path)

    def next_stage(self) -> None:
        phase = self._phase()
        if phase == "source-loaded":
            command = WorkerCommand.PREPROCESS
        elif phase in {"preprocessed", "mapping"}:
            command = WorkerCommand.ADVANCE_STAGE
        else:
            return
        self._run_worker(
            "Advancing compiler",
            lambda: self.worker.request(command),
            self._after_stage,
        )

    def continue_search(self) -> None:
        if not self._can_advance():
            return
        self._continuing = True
        self._continue_step()

    def compile_material(self) -> None:
        if self.source_path is None:
            return
        self._continuing = False
        self._run_worker(
            "Compiling material",
            lambda: self.worker.request(WorkerCommand.RUN_TO_MATERIAL),
            lambda event: self._accept_snapshot_event(
                event,
                select_candidate=False,
            ),
        )

    def place(self) -> None:
        if self._phase() != "materialized":
            return
        hierarchy_mode = self.physical_hierarchy_combo.currentText()
        self._run_worker(
            "Placing material",
            lambda: self.worker.request(
                WorkerCommand.PLACE,
                {
                    "column_pitch": self.column_pitch_spin.value(),
                    "row_pitch": self.row_pitch_spin.value(),
                    "routing_group_height": self.routing_group_height_spin.value(),
                    "routing_gap_rows": self.routing_gap_rows_spin.value(),
                    "physical_hierarchy": hierarchy_mode,
                    "hierarchy_threshold": (
                        self.hierarchy_threshold_spin.value()
                        if hierarchy_mode in {"min-objects", "min-cost"}
                        else None
                    ),
                    "generated_hierarchy": self.generated_hierarchy_combo.currentText(),
                },
            ),
            self._after_placement,
        )

    def stop(self) -> None:
        self._generation += 1
        self._continuing = False
        self.worker.stop()
        self._busy = False
        self.statusBar().showMessage("Stopped")
        self._update_actions()

    def fit_active_view(self) -> None:
        widget = self.content_tabs.currentWidget()
        if isinstance(widget, (YosysSvgView, VisualSceneView)):
            widget.fit_scene()
            return
        if isinstance(widget, QTabWidget):
            active = widget.currentWidget()
            if isinstance(active, VisualSceneView):
                active.fit_scene()

    def save_artifact(self, kind: str) -> None:
        if self.source_path is None:
            return
        default = self.source_path.with_suffix(f".{kind}.json")
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"Save {kind.title()}",
            str(default),
            "JSON files (*.json)",
        )
        if not path:
            return
        self._run_worker(
            f"Saving {kind}",
            lambda: self.worker.request(
                WorkerCommand.SAVE_ARTIFACT,
                {"kind": kind, "path": path},
            ),
            lambda event: self.statusBar().showMessage(
                f"Saved {_event_payload(event).get('path', path)}"
            ),
        )

    def export_lbp(self) -> None:
        if self.source_path is None:
            return
        default = self.source_path.with_suffix(".object.json")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export LBP Toolkit Plan",
            str(default),
            "JSON files (*.json)",
        )
        if not path:
            return
        self._run_worker(
            "Exporting LBP plan",
            lambda: self.worker.request(
                WorkerCommand.EXPORT_LBP_TOOLKIT,
                {
                    "path": path,
                    "title": None,
                    "description": None,
                    "creator": None,
                },
            ),
            lambda event: self.statusBar().showMessage(
                f"Exported {_event_payload(event).get('path', path)}"
            ),
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        self.stop()
        QThreadPool.globalInstance().waitForDone(2000)
        super().closeEvent(event)

    def _choose_source(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Verilog",
            str(self.source_path.parent if self.source_path else Path.cwd()),
            "Verilog sources (*.v *.sv);;All files (*)",
        )
        if path:
            self.open_source(path)

    def _continue_step(self) -> None:
        if not self._continuing or self._busy:
            return
        if not self._can_advance():
            self._continuing = False
            self._update_actions()
            self._select_frontier_candidate()
            return
        self.next_stage()

    def _after_stage(self, event: WorkerEvent) -> None:
        self._accept_snapshot_event(event, select_candidate=not self._continuing)
        if self._continuing:
            QTimer.singleShot(0, self._continue_step)

    def _after_placement(self, event: WorkerEvent) -> None:
        self._accept_snapshot_event(event, select_candidate=False)
        self._run_worker(
            "Building views",
            lambda: self.worker.request(
                WorkerCommand.BUILD_VISUAL_DOCUMENT,
                {"provider_options": {}},
            ),
            self._accept_visual_document,
        )

    def _accept_visual_document(self, event: WorkerEvent) -> None:
        payload = _event_payload(event)
        document_data = payload.get("document")
        self.set_visual_document(VisualDocument.from_canonical_data(document_data))
        self._apply_snapshot(_mapping(payload.get("snapshot")))

    def set_visual_document(self, document: VisualDocument) -> None:
        self._document = document
        for widget in self._visual_tabs:
            index = self.content_tabs.indexOf(widget)
            if index >= 0:
                self.content_tabs.removeTab(index)
            widget.deleteLater()
        self._visual_tabs.clear()
        self._scene_tabs.clear()
        self._scene_canvases.clear()
        for view in document.views:
            widget = self._visual_view_widget(view)
            self._visual_tabs.append(widget)
            label = "Placement" if view.identifier == "material" else view.label
            self.content_tabs.addTab(widget, label)
        if self._visual_tabs:
            self.content_tabs.setCurrentWidget(self._visual_tabs[-1])

    def _visual_view_widget(self, view: VisualView) -> QWidget:
        if len(view.scenes) == 1:
            canvas = self._scene_canvas(view.scenes[0])
            return canvas
        tabs = QTabWidget()
        for index, scene in enumerate(view.scenes):
            canvas = self._scene_canvas(scene)
            tabs.addTab(canvas, scene.label)
            self._scene_tabs[scene.identifier] = (tabs, index)
        return tabs

    def _scene_canvas(self, scene) -> VisualSceneView:
        canvas = VisualSceneView(scene)
        self._scene_canvases.append(canvas)
        canvas.set_overlays(
            wires=self.action_wires.isChecked(),
            labels=self.action_labels.isChecked(),
            bounds=self.action_bounds.isChecked(),
            collisions=self.action_collisions.isChecked(),
        )
        canvas.element_selected.connect(
            lambda identifier, value=scene, source=canvas: self._visual_element_selected(
                value,
                source,
                identifier,
            )
        )
        canvas.scene_activated.connect(self._activate_scene)
        return canvas

    def _activate_scene(self, identifier: str) -> None:
        target = self._scene_tabs.get(identifier)
        if target is None:
            return
        tabs, index = target
        self.content_tabs.setCurrentWidget(tabs)
        tabs.setCurrentIndex(index)

    def _visual_element_selected(
        self,
        scene,
        canvas: VisualSceneView,
        identifier: str,
    ) -> None:
        if not identifier:
            self._inspector_canvas = None
            return
        self._inspector_canvas = canvas
        element: VisualElement = scene.element(identifier)
        connected_nets = scene.nets_for_element(identifier)
        properties = [
            ("Identity", "ID", element.identifier),
            ("Geometry", "X", f"{element.transform.x:g}"),
            ("Geometry", "Y", f"{element.transform.y:g}"),
            ("Geometry", "Angle", f"{element.transform.angle:g}"),
        ]
        properties.extend(
            (item.group, item.name, item.value)
            for item in element.descriptor.properties
        )
        properties.extend(
            ("Reference", item.kind, item.identifier) for item in element.references
        )
        properties.extend(
            (
                "Connected net",
                net.label,
                f"{net.identifier} ({len(net.endpoints)} endpoints)",
            )
            for net in connected_nets
        )
        items = self._show_properties(element.descriptor.label, properties)
        for item, net in zip(items[-len(connected_nets) :], connected_nets):
            item.setData(0, Qt.ItemDataRole.UserRole, net.identifier)

    def _inspector_selected(
        self,
        item: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        if item is None or self._inspector_canvas is None:
            return
        identifier = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(identifier, str):
            self._inspector_canvas.highlight_net(identifier)

    def _apply_overlays(self) -> None:
        for canvas in self._scene_canvases:
            canvas.set_overlays(
                wires=self.action_wires.isChecked(),
                labels=self.action_labels.isChecked(),
                bounds=self.action_bounds.isChecked(),
                collisions=self.action_collisions.isChecked(),
            )

    def _candidate_selected(self, current, _previous) -> None:
        identifier = self._candidate_model.candidate_id(current)
        if identifier is None or self._busy:
            return
        self._inspector_canvas = None
        source = current.siblingAtColumn(0).data(SOURCE_CANDIDATE_ROLE)
        status = current.siblingAtColumn(1).data(Qt.ItemDataRole.DisplayRole)
        stage = current.siblingAtColumn(2).data(Qt.ItemDataRole.DisplayRole)
        self._proposal_model.set_snapshot(
            self.snapshot or {},
            candidate_id=source if isinstance(source, str) and source else identifier,
        )
        checkpoint = self._candidate_model.checkpoint_digest(current)

        def accepted(event: WorkerEvent) -> None:
            details = _mapping(_event_payload(event).get("candidate"))
            self._claims_model.set_candidate_details(details)
            candidate = _mapping(details.get("candidate"))
            properties = [
                ("Candidate", "ID", str(candidate.get("identifier", ""))),
                ("Candidate", "Status", str(status or "")),
                ("Candidate", "Parent", str(source or "initial")),
                ("Candidate", "Checkpoint", str(checkpoint or "")),
                ("Candidate", "State", str(details.get("state_digest", ""))),
                ("Search", "Stage", str(stage or "")),
                ("Cost", "Lower bound", str(candidate.get("lower_bound", ""))),
                ("Cost", "Expected", str(candidate.get("expected_cost", ""))),
                ("Search", "Decisions", str(candidate.get("decision_count", ""))),
                ("Search", "Claims", str(candidate.get("claim_count", ""))),
            ]
            for decision in _sequence(details.get("decisions")):
                decision_data = _mapping(decision)
                decision_stage = str(decision_data.get("stage", ""))
                accepted = ", ".join(
                    str(value) for value in _sequence(decision_data.get("accepted"))
                )
                deferred = ", ".join(
                    str(value) for value in _sequence(decision_data.get("deferred"))
                )
                properties.extend(
                    (
                        (f"Decision: {decision_stage}", "Accepted", accepted or "none"),
                        (f"Decision: {decision_stage}", "Deferred", deferred or "none"),
                    )
                )
            self._show_properties("Candidate", properties)
            if checkpoint is not None:
                self._request_schematic(checkpoint)

        self._run_worker(
            "Loading candidate",
            lambda: self.worker.request(
                WorkerCommand.CANDIDATE_DETAILS,
                {"identifier": identifier},
            ),
            accepted,
        )

    def _pipeline_selected(self, item: QTreeWidgetItem | None) -> None:
        if item is None or self._busy:
            return
        digest = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(digest, str) and digest:
            self._request_schematic(digest)

    def _request_schematic(self, checkpoint_digest: str) -> None:
        self._run_worker(
            "Rendering Yosys schematic",
            lambda: self.worker.request(
                WorkerCommand.RENDER_SCHEMATIC,
                {
                    "checkpoint_digest": checkpoint_digest,
                    "module": None,
                    "options": {
                        "show_widths": False,
                        "stretch_ports": False,
                    },
                },
            ),
            self._accept_schematic,
        )

    def _accept_schematic(self, event: WorkerEvent) -> None:
        schematic = _mapping(_event_payload(event).get("schematic"))
        svg = schematic.get("svg")
        if isinstance(svg, str):
            self.yosys_view.load_svg(svg)
            self.content_tabs.setCurrentWidget(self.yosys_view)
            module = schematic.get("module", "")
            self.statusBar().showMessage(f"Yosys schematic: {module}")

    def _accept_snapshot_event(
        self,
        event: WorkerEvent,
        *,
        select_candidate: bool = True,
    ) -> None:
        snapshot = _mapping(_event_payload(event).get("snapshot"))
        self._apply_snapshot(snapshot)
        if select_candidate:
            self._select_frontier_candidate()

    def _apply_snapshot(self, snapshot: Mapping[str, object]) -> None:
        self.snapshot = dict(snapshot)
        phase = self._phase()
        self.phase_label.setText(phase.replace("-", " ").title())
        self._candidate_model.set_snapshot(snapshot)
        self.candidates.expandAll()
        self._proposal_model.set_snapshot(snapshot)
        self._update_pipeline(snapshot)
        self._update_artifacts(snapshot)
        self._update_actions()
        self.snapshot_changed.emit(self.snapshot)

    def _update_pipeline(self, snapshot: Mapping[str, object]) -> None:
        self.pipeline.clear()
        QTreeWidgetItem(self.pipeline, ("Source", "loaded", "1"))
        for stage in _sequence(snapshot.get("stages")):
            stage_data = _mapping(stage)
            item = QTreeWidgetItem(
                self.pipeline,
                (
                    str(stage_data.get("name", "")),
                    "complete",
                    str(stage_data.get("output_candidates", "")),
                ),
            )
            checkpoints = _sequence(stage_data.get("post_pass_checkpoints"))
            if checkpoints:
                checkpoint = _mapping(checkpoints[0]).get("checkpoint")
                if isinstance(checkpoint, str):
                    item.setData(0, Qt.ItemDataRole.UserRole, checkpoint)
        next_stage = snapshot.get("next_stage")
        if isinstance(next_stage, str):
            QTreeWidgetItem(self.pipeline, (next_stage, "next", ""))

    def _update_artifacts(self, snapshot: Mapping[str, object]) -> None:
        self.material_summary.clear()
        artifacts = _mapping(snapshot.get("artifacts"))
        for kind in ("material", "placement", "visualization"):
            value = artifacts.get(kind)
            if not isinstance(value, Mapping):
                continue
            item = QTreeWidgetItem(
                self.material_summary,
                (kind.title(), str(value.get("digest") or "in memory")),
            )
            for name, detail in _mapping(value.get("details")).items():
                QTreeWidgetItem(item, (str(name), str(detail)))
        self.material_summary.expandAll()

    def _select_frontier_candidate(self) -> None:
        if self._candidate_model.rowCount() == 0:
            return
        group = self._candidate_model.item(self._candidate_model.rowCount() - 1)
        if group is None or group.rowCount() == 0:
            return
        self.candidates.setCurrentIndex(group.child(0).index())

    def _show_properties(
        self,
        title: str,
        values: Sequence[tuple[str, str, str]],
    ) -> list[QTreeWidgetItem]:
        self.inspector.clear()
        root = QTreeWidgetItem(self.inspector, (title, ""))
        groups: dict[str, QTreeWidgetItem] = {}
        items = []
        for group_name, name, value in values:
            group = groups.get(group_name)
            if group is None:
                group = QTreeWidgetItem(root, (group_name, ""))
                groups[group_name] = group
            items.append(QTreeWidgetItem(group, (name, value)))
        self.inspector.expandAll()
        return items

    def _run_worker(
        self,
        label: str,
        operation: Callable[[], object],
        accepted: Callable[[WorkerEvent], None],
    ) -> None:
        if self._busy:
            self.statusBar().showMessage("Compiler worker is busy")
            return
        self._busy = True
        self._update_actions()
        self.statusBar().showMessage(label)
        generation = self._generation
        task = _WorkerTask(operation)
        self._tasks.add(task)

        def completed(value: object) -> None:
            self._tasks.discard(task)
            if generation != self._generation:
                return
            self._busy = False
            if not isinstance(value, WorkerEvent):
                self._task_failed(TypeError("Worker returned an invalid event"))
                return
            accepted(value)
            self._update_actions()
            self.operation_finished.emit(label)

        def failed(error: object) -> None:
            self._tasks.discard(task)
            if generation != self._generation:
                return
            self._busy = False
            self._task_failed(error)
            self.operation_finished.emit(label)

        task.signals.completed.connect(completed)
        task.signals.failed.connect(failed)
        QThreadPool.globalInstance().start(task)

    def _task_failed(self, error: object) -> None:
        self._continuing = False
        self.statusBar().showMessage(f"Operation failed: {error}")
        self._update_actions()

    def _phase(self) -> str:
        if self.snapshot is None:
            return ""
        value = self.snapshot.get("phase")
        return value if isinstance(value, str) else ""

    def _can_advance(self) -> bool:
        return self._phase() in {"source-loaded", "preprocessed", "mapping"}

    def _update_actions(self) -> None:
        has_source = self.source_path is not None and self.snapshot is not None
        phase = self._phase()
        artifacts = _mapping((self.snapshot or {}).get("artifacts"))
        self.action_reload.setEnabled(self.source_path is not None and not self._busy)
        self.action_next.setEnabled(has_source and self._can_advance() and not self._busy)
        self.action_continue.setEnabled(
            has_source and self._can_advance() and not self._busy
        )
        self.action_stop.setEnabled(self.worker.alive)
        self.action_compile.setEnabled(
            has_source
            and phase in {"source-loaded", "preprocessed", "mapping", "search-complete"}
            and not self._busy
        )
        self.action_place.setEnabled(phase == "materialized" and not self._busy)
        placement_editable = (
            has_source
            and phase not in {"placed", "realized"}
            and not self._busy
        )
        for control in self._placement_controls:
            control.setEnabled(placement_editable)
        self.hierarchy_threshold_spin.setEnabled(
            placement_editable
            and self.physical_hierarchy_combo.currentText()
            in {"min-objects", "min-cost"}
        )
        self.save_actions["material"].setEnabled(
            isinstance(artifacts.get("material"), Mapping) and not self._busy
        )
        self.save_actions["state"].setEnabled(
            isinstance(artifacts.get("material"), Mapping) and not self._busy
        )
        self.save_actions["report"].setEnabled(
            phase in {"search-complete", "materialized", "placed", "realized"}
            and not self._busy
        )
        self.save_actions["placement"].setEnabled(
            isinstance(artifacts.get("placement"), Mapping) and not self._busy
        )
        self.save_actions["visualization"].setEnabled(
            isinstance(artifacts.get("visualization"), Mapping) and not self._busy
        )
        self.action_export.setEnabled(
            isinstance(artifacts.get("placement"), Mapping) and not self._busy
        )

    def _clear_session_views(self) -> None:
        self.pipeline.clear()
        self.material_summary.clear()
        self._candidate_model.set_snapshot({})
        self._proposal_model.set_snapshot({})
        self._claims_model.set_candidate_details(None)
        self.inspector.clear()
        self._inspector_canvas = None
        self.set_visual_document_empty()

    def set_visual_document_empty(self) -> None:
        for widget in self._visual_tabs:
            index = self.content_tabs.indexOf(widget)
            if index >= 0:
                self.content_tabs.removeTab(index)
            widget.deleteLater()
        self._visual_tabs.clear()
        self._scene_tabs.clear()
        self._scene_canvases.clear()
        self._document = None


def _toolbar_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("toolbarLabel")
    return label


def _placement_number_control(
    object_name: str,
    value: float,
    step: float,
    tooltip: str,
) -> QDoubleSpinBox:
    control = QDoubleSpinBox()
    control.setObjectName(object_name)
    control.setRange(0.01, 1_000_000.0)
    control.setDecimals(2)
    control.setSingleStep(step)
    control.setValue(value)
    control.setToolTip(tooltip)
    return control


def _panel_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("panelLabel")
    label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return label


def _toggle_action(
    text: str,
    object_name: str,
    checked: bool,
    parent: QObject,
) -> QAction:
    action = QAction(text, parent)
    action.setObjectName(object_name)
    action.setCheckable(True)
    action.setChecked(checked)
    return action


def _table(model, object_name: str) -> QTableView:
    table = QTableView()
    table.setObjectName(object_name)
    table.setModel(model)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
    table.setSortingEnabled(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    return table


def _event_payload(event: WorkerEvent) -> dict[str, JsonValue]:
    return event.payload


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, Sequence) and not isinstance(value, str) else ()


def launch_workbench(source: str | Path | None = None) -> int:
    application = QApplication.instance()
    owns_application = application is None
    if application is None:
        application = QApplication(sys.argv[:1])
    application.setApplicationName("GateForge Workbench")
    application.setOrganizationName("GateForge")
    window = WorkbenchWindow(source)
    window.show()
    if not owns_application:
        return 0
    return application.exec()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gateforge workbench",
        description="Open the GateForge compiler workbench.",
    )
    parser.add_argument("source", nargs="?", type=Path)
    args = parser.parse_args(argv)
    return launch_workbench(args.source)