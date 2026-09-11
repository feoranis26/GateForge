from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QSplitter,
    QTabWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gateforge.visualization.interaction import find_collisions
from gateforge.visualization.model import (
    VisualDocument,
    VisualElement,
    VisualScene,
)
from gateforge.workbench.qt_canvas import VisualSceneView


class VisualizerWindow(QMainWindow):
    def __init__(
        self,
        document: VisualDocument,
        *,
        reload_document: Callable[[], VisualDocument] | None = None,
        watch_paths: Sequence[Path] = (),
        watch: bool = True,
    ) -> None:
        super().__init__()
        self.setObjectName("visualizerWindow")
        self.setWindowTitle("GateForge Visualizer")
        self.resize(1440, 900)
        self.setMinimumSize(900, 560)
        self.document = document
        self.reload_document = reload_document
        self.watch_paths = tuple(watch_paths)
        self._watch_signature = self._file_signature()
        self._view_by_id = {
            view.identifier: view for view in document.views
        }
        self._canvases: dict[str, VisualSceneView] = {}
        self._scenes: dict[str, VisualScene] = {}
        self._scene_indexes: dict[str, int] = {}
        self._inspected_scene: str | None = None
        self._build_actions(watch)
        self._build_ui()
        self.set_document(document)
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(750)
        self._watch_timer.timeout.connect(self._poll_files)
        self._watch_timer.start()

    def _build_actions(self, watch: bool) -> None:
        self.action_reload = QAction("Reload", self)
        self.action_reload.setObjectName("actionReload")
        self.action_reload.setEnabled(self.reload_document is not None)
        self.action_reload.triggered.connect(self.reload)

        self.action_fit = QAction("Fit", self)
        self.action_fit.setObjectName("actionFit")
        self.action_fit.triggered.connect(self.fit_active)

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
            action.toggled.connect(self._apply_overlays)

        self.action_watch = _toggle_action("Watch", "actionWatch", watch, self)
        self.action_watch.setEnabled(
            self.reload_document is not None and bool(self.watch_paths)
        )

    def _build_ui(self) -> None:
        toolbar = QToolBar("Viewer", self)
        toolbar.setObjectName("viewerToolbar")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        toolbar.addWidget(QLabel("View"))
        self.view_selector = QComboBox()
        self.view_selector.setObjectName("viewSelector")
        self.view_selector.setMinimumContentsLength(20)
        self.view_selector.currentIndexChanged.connect(self._selected_view_changed)
        toolbar.addWidget(self.view_selector)
        toolbar.addSeparator()
        toolbar.addAction(self.action_reload)
        toolbar.addAction(self.action_fit)
        toolbar.addSeparator()
        toolbar.addAction(self.action_wires)
        toolbar.addAction(self.action_labels)
        toolbar.addAction(self.action_bounds)
        toolbar.addAction(self.action_collisions)
        toolbar.addAction(self.action_watch)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.scene_tabs = QTabWidget()
        self.scene_tabs.setObjectName("sceneTabs")
        self.scene_tabs.currentChanged.connect(self._scene_changed)
        splitter.addWidget(self.scene_tabs)

        inspector = QWidget()
        inspector.setObjectName("inspector")
        inspector_layout = QVBoxLayout(inspector)
        inspector_layout.addWidget(QLabel("Selection"))
        self.selection_label = QLabel("Nothing selected")
        self.selection_label.setWordWrap(True)
        inspector_layout.addWidget(self.selection_label)
        self.properties = QTreeWidget()
        self.properties.setObjectName("properties")
        self.properties.setHeaderLabels(("Property", "Value"))
        self.properties.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.properties.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        inspector_layout.addWidget(self.properties, 1)
        inspector_layout.addWidget(QLabel("Connected nets"))
        self.nets = QListWidget()
        self.nets.setObjectName("connectedNets")
        self.nets.setMaximumHeight(180)
        self.nets.itemSelectionChanged.connect(self._select_net)
        inspector_layout.addWidget(self.nets)
        splitter.addWidget(inspector)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)
        self.statusBar().showMessage("Ready")

    def set_document(self, document: VisualDocument) -> None:
        previous = self.view_selector.currentData()
        self.document = document
        self._view_by_id = {
            view.identifier: view for view in document.views
        }
        selected = (
            previous
            if isinstance(previous, str) and previous in self._view_by_id
            else document.views[0].identifier
        )
        self.view_selector.blockSignals(True)
        self.view_selector.clear()
        for view in document.views:
            self.view_selector.addItem(view.label, view.identifier)
        self.view_selector.setCurrentIndex(self.view_selector.findData(selected))
        self.view_selector.blockSignals(False)
        self._show_view(selected)

    def reload(self) -> None:
        if self.reload_document is None:
            self.statusBar().showMessage("No reload source is configured")
            return
        try:
            document = self.reload_document()
        except Exception as error:
            self.statusBar().showMessage(f"Reload failed: {error}")
            return
        self.set_document(document)
        self._watch_signature = self._file_signature()
        self.statusBar().showMessage(
            "Reloaded material, placement, and realized views"
        )

    def fit_active(self) -> None:
        canvas = self._active_canvas()
        if canvas is not None:
            canvas.fit_scene()

    def _selected_view_changed(self) -> None:
        identifier = self.view_selector.currentData()
        if isinstance(identifier, str):
            self._show_view(identifier)

    def _show_view(self, identifier: str) -> None:
        view = self._view_by_id[identifier]
        while self.scene_tabs.count():
            widget = self.scene_tabs.widget(0)
            self.scene_tabs.removeTab(0)
            widget.deleteLater()
        self._canvases.clear()
        self._scenes = {scene.identifier: scene for scene in view.scenes}
        self._scene_indexes.clear()
        for scene in view.scenes:
            canvas = VisualSceneView(scene)
            canvas.element_selected.connect(
                lambda element, scene_id=scene.identifier: self._inspect(
                    scene_id, element
                )
            )
            canvas.scene_activated.connect(self._activate_scene)
            self._canvases[scene.identifier] = canvas
            index = self.scene_tabs.addTab(canvas, scene.label)
            self._scene_indexes[scene.identifier] = index
        self._apply_overlays()
        self._clear_inspector()
        collisions = sum(len(find_collisions(scene)) for scene in view.scenes)
        self.statusBar().showMessage(
            f"{view.label}: {len(view.scenes)} scene(s), "
            f"{sum(len(scene.elements) for scene in view.scenes)} elements, "
            f"{collisions} collision pair(s)"
        )

    def _apply_overlays(self) -> None:
        for canvas in self._canvases.values():
            canvas.set_overlays(
                wires=self.action_wires.isChecked(),
                labels=self.action_labels.isChecked(),
                bounds=self.action_bounds.isChecked(),
                collisions=self.action_collisions.isChecked(),
            )

    def _inspect(self, scene_id: str, element_id: str) -> None:
        self._clear_inspector()
        if not element_id:
            return
        scene = self._scenes[scene_id]
        element = scene.element(element_id)
        self._inspected_scene = scene_id
        self.selection_label.setText(element.descriptor.label)
        for property_ in element.descriptor.properties:
            self.properties.addTopLevelItem(
                QTreeWidgetItem(
                    (f"{property_.group}: {property_.name}", property_.value)
                )
            )
        for reference in element.references:
            self.properties.addTopLevelItem(
                QTreeWidgetItem(
                    (f"Reference: {reference.kind}", reference.identifier)
                )
            )
        bounds = element.world_bounds
        self.properties.addTopLevelItem(
            QTreeWidgetItem(
                ("Position", f"{element.transform.x:g}, {element.transform.y:g}")
            )
        )
        self.properties.addTopLevelItem(
            QTreeWidgetItem(("Size", f"{bounds.width:g} x {bounds.height:g}"))
        )
        for net in scene.nets_for_element(element.identifier):
            list_item = QListWidgetItem(net.label)
            list_item.setData(Qt.ItemDataRole.UserRole, net.identifier)
            self.nets.addItem(list_item)

    def _clear_inspector(self) -> None:
        self._inspected_scene = None
        self.selection_label.setText("Nothing selected")
        self.properties.clear()
        self.nets.clear()
        for canvas in self._canvases.values():
            canvas.highlight_net(None)

    def _select_net(self) -> None:
        item = self.nets.currentItem()
        if item is None or self._inspected_scene is None:
            return
        identifier = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(identifier, str):
            return
        canvas = self._canvases[self._inspected_scene]
        canvas.highlight_net(identifier)
        net = next(
            item
            for item in self._scenes[self._inspected_scene].nets
            if item.identifier == identifier
        )
        self.statusBar().showMessage(
            f"{net.label}: {len(net.endpoints)} endpoint(s)"
        )

    def _activate_scene(self, identifier: str) -> None:
        index = self._scene_indexes.get(identifier)
        if index is not None:
            self.scene_tabs.setCurrentIndex(index)

    def _scene_changed(self) -> None:
        self._clear_inspector()

    def _active_canvas(self) -> VisualSceneView | None:
        widget = self.scene_tabs.currentWidget()
        return widget if isinstance(widget, VisualSceneView) else None

    def _file_signature(self) -> tuple[tuple[str, int, int], ...]:
        result = []
        for path in self.watch_paths:
            try:
                stat = path.stat()
                result.append((str(path), stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                result.append((str(path), -1, -1))
        return tuple(result)

    def _poll_files(self) -> None:
        if self.action_watch.isChecked() and self.reload_document is not None:
            signature = self._file_signature()
            if signature != self._watch_signature:
                self.reload()


def _toggle_action(
    text: str,
    object_name: str,
    checked: bool,
    parent: QMainWindow,
) -> QAction:
    action = QAction(text, parent)
    action.setObjectName(object_name)
    action.setCheckable(True)
    action.setChecked(checked)
    return action


_open_windows: list[VisualizerWindow] = []


def launch_visualizer(
    document: VisualDocument,
    *,
    reload_document: Callable[[], VisualDocument] | None = None,
    watch_paths: Sequence[Path] = (),
    watch: bool = True,
) -> int:
    application = QApplication.instance()
    owns_application = application is None
    if application is None:
        application = QApplication(sys.argv[:1])
        application.setApplicationName("GateForge Visualizer")
        application.setOrganizationName("GateForge")
    window = VisualizerWindow(
        document,
        reload_document=reload_document,
        watch_paths=watch_paths,
        watch=watch,
    )
    _open_windows.append(window)
    window.destroyed.connect(
        lambda: _open_windows.remove(window) if window in _open_windows else None
    )
    window.show()
    if not owns_application:
        return 0
    return application.exec()