from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from gateforge.visualization.interaction import find_collisions
from gateforge.visualization.model import VisualDocument, VisualElement, VisualScene
from gateforge.visualization.tk_canvas import SceneCanvas


class VisualizerApp(tk.Tk):
    def __init__(
        self,
        document: VisualDocument,
        *,
        reload_document: Callable[[], VisualDocument] | None = None,
        watch_paths: Sequence[Path] = (),
        watch: bool = True,
    ) -> None:
        super().__init__()
        self.title("GateForge Visualizer")
        self.geometry("1440x900")
        self.minsize(900, 560)
        self.configure(background="#101816")
        self.document = document
        self.reload_document = reload_document
        self.watch_paths = tuple(watch_paths)
        self._watch_signature = self._file_signature()
        self._canvases: dict[str, SceneCanvas] = {}
        self._scenes: dict[str, VisualScene] = {}
        self._view_by_label = {view.label: view for view in document.views}
        self._status = tk.StringVar(value="Ready")
        self._view = tk.StringVar(value=document.views[0].label)
        self._watch = tk.BooleanVar(value=watch)
        self._wires = tk.BooleanVar(value=True)
        self._labels = tk.BooleanVar(value=True)
        self._bounds = tk.BooleanVar(value=False)
        self._collisions = tk.BooleanVar(value=True)
        self._build_style()
        self._build_ui()
        self._show_view(document.views[0].label)
        self.after(750, self._poll_files)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background="#18241f")
        style.configure("Toolbar.TFrame", background="#24362f")
        style.configure("TLabel", background="#18241f", foreground="#e8eee9")
        style.configure("Status.TLabel", background="#24362f", foreground="#b9cbc3")
        style.configure("TCheckbutton", background="#24362f", foreground="#e8eee9")
        style.configure("TNotebook", background="#18241f", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(12, 6))
        style.configure("Treeview", rowheight=24)

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, style="Toolbar.TFrame", padding=(10, 8))
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text="View", style="Status.TLabel").pack(side=tk.LEFT)
        selector = ttk.Combobox(
            toolbar,
            textvariable=self._view,
            values=tuple(self._view_by_label),
            state="readonly",
            width=24,
        )
        selector.pack(side=tk.LEFT, padx=(8, 14))
        selector.bind("<<ComboboxSelected>>", lambda _event: self._show_view(self._view.get()))
        ttk.Button(toolbar, text="Reload", command=self.reload).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Fit", command=self.fit_active).pack(side=tk.LEFT, padx=(6, 14))
        for text, variable in (
            ("Wires", self._wires),
            ("Labels", self._labels),
            ("Bounds", self._bounds),
            ("Collisions", self._collisions),
            ("Watch", self._watch),
        ):
            ttk.Checkbutton(
                toolbar,
                text=text,
                variable=variable,
                command=self._apply_overlays,
            ).pack(side=tk.LEFT, padx=4)

        body = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)
        center = ttk.Frame(body)
        inspector = ttk.Frame(body, padding=10)
        body.add(center, weight=4)
        body.add(inspector, weight=1)
        self.notebook = ttk.Notebook(center)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", lambda _event: self._clear_inspector())

        ttk.Label(inspector, text="Selection", font=("TkHeadingFont", 12, "bold")).pack(anchor=tk.W)
        self.selection_label = ttk.Label(inspector, text="Nothing selected", wraplength=320)
        self.selection_label.pack(fill=tk.X, pady=(4, 8))
        self.properties = ttk.Treeview(
            inspector,
            columns=("property", "value"),
            show="headings",
            height=14,
        )
        self.properties.heading("property", text="Property")
        self.properties.heading("value", text="Value")
        self.properties.column("property", width=105, stretch=False)
        self.properties.column("value", width=220, stretch=True)
        self.properties.pack(fill=tk.BOTH, expand=True)
        ttk.Label(inspector, text="Connected nets", font=("TkHeadingFont", 11, "bold")).pack(
            anchor=tk.W, pady=(12, 4)
        )
        self.nets = tk.Listbox(
            inspector,
            height=8,
            background="#edf2ef",
            foreground="#17231f",
            selectbackground="#d4a72c",
            activestyle="none",
        )
        self.nets.pack(fill=tk.X)
        self.nets.bind("<<ListboxSelect>>", self._select_net)
        ttk.Label(self, textvariable=self._status, style="Status.TLabel", padding=(10, 5)).pack(fill=tk.X)

    def set_document(self, document: VisualDocument) -> None:
        previous = self._view.get()
        self.document = document
        self._view_by_label = {view.label: view for view in document.views}
        selected = previous if previous in self._view_by_label else document.views[0].label
        self._view.set(selected)
        self._show_view(selected)

    def reload(self) -> None:
        if self.reload_document is None:
            self._status.set("No reload source is configured")
            return
        try:
            document = self.reload_document()
        except Exception as error:
            self._status.set(f"Reload failed: {error}")
            return
        self.set_document(document)
        self._watch_signature = self._file_signature()
        self._status.set("Reloaded material, placement, and realized views")

    def fit_active(self) -> None:
        canvas = self._active_canvas()
        if canvas is not None:
            canvas.fit_scene()

    def _show_view(self, label: str) -> None:
        view = self._view_by_label[label]
        for tab in self.notebook.tabs():
            self.notebook.forget(tab)
        self._canvases.clear()
        self._scenes = {scene.identifier: scene for scene in view.scenes}
        for scene in view.scenes:
            frame = ttk.Frame(self.notebook)
            canvas = SceneCanvas(
                frame,
                scene,
                on_select=lambda element, scene_id=scene.identifier: self._inspect(scene_id, element),
                on_activate=self._activate_element,
            )
            canvas.pack(fill=tk.BOTH, expand=True)
            self.notebook.add(frame, text=scene.label)
            self._canvases[scene.identifier] = canvas
        self._apply_overlays()
        self._clear_inspector()
        collisions = sum(len(find_collisions(scene)) for scene in view.scenes)
        self._status.set(
            f"{view.label}: {len(view.scenes)} scene(s), "
            f"{sum(len(scene.elements) for scene in view.scenes)} elements, "
            f"{collisions} collision pair(s)"
        )

    def _apply_overlays(self) -> None:
        for canvas in self._canvases.values():
            canvas.set_overlays(
                wires=self._wires.get(),
                labels=self._labels.get(),
                bounds=self._bounds.get(),
                collisions=self._collisions.get(),
            )

    def _inspect(self, scene_id: str, element: VisualElement | None) -> None:
        self._clear_inspector()
        if element is None:
            return
        self.selection_label.configure(text=element.descriptor.label)
        for property_ in element.descriptor.properties:
            self.properties.insert(
                "",
                tk.END,
                values=(f"{property_.group}: {property_.name}", property_.value),
            )
        for reference in element.references:
            self.properties.insert(
                "",
                tk.END,
                values=(f"Reference: {reference.kind}", reference.identifier),
            )
        bounds = element.world_bounds
        self.properties.insert(
            "",
            tk.END,
            values=("Position", f"{element.transform.x:g}, {element.transform.y:g}"),
        )
        self.properties.insert(
            "",
            tk.END,
            values=("Size", f"{bounds.width:g} x {bounds.height:g}"),
        )
        scene = self._scenes[scene_id]
        for net in scene.nets_for_element(element.identifier):
            self.nets.insert(tk.END, net.label)
            self.nets.itemconfig(tk.END, foreground="#17231f")
        self.nets._gateforge_scene = scene_id
        self.nets._gateforge_element = element.identifier

    def _clear_inspector(self) -> None:
        self.selection_label.configure(text="Nothing selected")
        self.properties.delete(*self.properties.get_children())
        self.nets.delete(0, tk.END)
        for canvas in self._canvases.values():
            canvas.highlight_net(None)

    def _select_net(self, _event) -> None:
        selection = self.nets.curselection()
        if not selection:
            return
        scene_id = getattr(self.nets, "_gateforge_scene", None)
        element_id = getattr(self.nets, "_gateforge_element", None)
        if scene_id is None or element_id is None:
            return
        scene = self._scenes[scene_id]
        connected = scene.nets_for_element(element_id)
        net = connected[selection[0]]
        self._canvases[scene_id].highlight_net(net.identifier)
        self._status.set(
            f"{net.label}: {len(net.endpoints)} endpoint(s)"
        )

    def _activate_element(self, element: VisualElement) -> None:
        child = element.linked_scene
        if child is None or child not in self._canvases:
            return
        target = self._canvases[child].master
        self.notebook.select(target)

    def _active_canvas(self) -> SceneCanvas | None:
        selected = self.notebook.select()
        if not selected:
            return None
        frame = self.nametowidget(selected)
        return next(
            (child for child in frame.winfo_children() if isinstance(child, SceneCanvas)),
            None,
        )

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
        if self._watch.get() and self.reload_document is not None:
            signature = self._file_signature()
            if signature != self._watch_signature:
                self.reload()
        self.after(750, self._poll_files)


def launch_visualizer(
    document: VisualDocument,
    *,
    reload_document: Callable[[], VisualDocument] | None = None,
    watch_paths: Sequence[Path] = (),
    watch: bool = True,
) -> None:
    try:
        app = VisualizerApp(
            document,
            reload_document=reload_document,
            watch_paths=watch_paths,
            watch=watch,
        )
    except tk.TclError as error:
        raise RuntimeError(f"Unable to open GateForge visualizer: {error}") from error
    app.mainloop()