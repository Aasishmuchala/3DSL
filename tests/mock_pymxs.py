"""Hostile-mock ``pymxs`` runtime — proves ``maxgaffer.maxbridge`` can never crash Max.

The bridge treats every ``rt.*`` call as suspect (deleted nodes, missing V-Ray,
locked properties). This harness makes that threat model executable, off-Max:

* ``MockObject`` / ``MockNode`` — MAXScript value bags with a class tag, a property
  bag, and hostility controls: per-property ``arm_get``/``arm_set`` (raise on
  access), ``set_undefined`` (read back MAXScript ``undefined``), and
  ``set_stale`` (deleted-node handle — *every* access raises ``MockRtError``).
* ``FakeMaxRuntime`` — the fake ``pymxs.runtime``: class registry
  (``classOf``/``superClassOf``/``isProperty``/``isValidNode``/``getNodeByName``),
  ``cameras``/``lights`` collections that can break mid-iteration,
  ``SceneExposureControl`` / ``renderers.current`` / ``environmentMap`` /
  ``viewport`` / ``LayerManager`` / ``renderSceneDialog`` / ``actionMan`` /
  ``currentTime`` / ``objects``, class + function makers (``VRayBitmap``,
  ``VRayLight``, ``vrayCreateVRayExposureControl`` …) that can be absent or armed,
  ``execute()``, ``undo()``/``redo()`` and a recording ``pymxs.undo`` context
  manager.
* ``mutation_log`` — every scene mutation the bridge causes:
  ``("set", obj, prop, old, new)``, ``("set_global", name, old, new)``,
  ``("create", cls, obj)``, ``("delete", obj)``, ``("execute", expr)``,
  ``("undo_enter"|"undo_exit", label[, status])``, ``("layer_add", layer, node)``,
  ``("viewport_setCamera", cam)``, ``("render"|"save"|"close"|"redrawViews", …)``.
  Failure-path tests assert on this log: *no partial mutation* means no entry.
* Chaos mode — three seeded knobs (deterministic for a fixed ``seed``):
  ``chaos_rt`` (runtime service calls: classOf, isProperty, getPropNames,
  getNodeByName, execute, plugin-class makers, render/bitmap I/O, collection
  iteration, managed globals), ``chaos_set`` (node property writes — locked
  properties), ``chaos_get`` (node property reads — off by default; arm specific
  reads deterministically with ``arm_get`` instead). Pure value constructors
  (``Point3``/``Point2``/``color``/matrix helpers/``Name``) never chaos-fail:
  in real Max they are in-process value objects, not scene I/O.

Stdlib-only, Python 3.11+ (Max 2026) compatible. Deterministic for a fixed seed.
"""

from __future__ import annotations

import random
import types
from typing import Any, Callable, Dict, List, Optional

#: Default deterministic seed for the whole hostile suite.
CHAOS_SEED = 20260716


class MockRtError(RuntimeError):
    """Stand-in for the RuntimeError pymxs raises on any MAXScript failure."""


class _Undefined:
    """MAXScript ``undefined``: ``is not None``, falsy, str()s as ``'undefined'``."""

    _instance: Optional["_Undefined"] = None

    def __new__(cls) -> "_Undefined":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "undefined"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return False


UNDEFINED = _Undefined()


# --------------------------------------------------------------------- values
class MockPoint3:
    def __init__(self, x: float, y: float, z: float):
        self.x, self.y, self.z = float(x), float(y), float(z)

    def __sub__(self, other: "MockPoint3") -> "MockPoint3":
        return MockPoint3(self.x - other.x, self.y - other.y, self.z - other.z)

    def __repr__(self) -> str:
        return "Point3({0.x:g}, {0.y:g}, {0.z:g})".format(self)


class MockPoint2:
    def __init__(self, x: float, y: float):
        self.x, self.y = float(x), float(y)


class MockColor:
    def __init__(self, r: float, g: float, b: float):
        self.r, self.g, self.b = float(r), float(g), float(b)


class MockTextureMap:
    """Superclass marker: ``rt.superClassOf(texmap) == rt.textureMap``."""


class MockMatrix3:
    """Transform stand-in: row3/translationpart/rotationpart + a controller whose
    class reads 'Position_XYZ' (a healthy scripted sun; classify_rig notes on
    anything else). ``__mul__`` is a composition stand-in — rotation VALUES are
    not asserted, only that the bridge's math path never raises."""

    def __init__(self, rt: "FakeMaxRuntime", row3: Optional[MockPoint3] = None,
                 translation: Optional[MockPoint3] = None):
        self.row3 = row3 or MockPoint3(0.0, -1.0, 0.0)
        self.translationpart = translation or MockPoint3(0.0, 0.0, 0.0)
        self.rotationpart = object()                      # quaternion stand-in
        self.controller = MockObject(rt, "Position_XYZ")

    def __mul__(self, other: "MockMatrix3") -> "MockMatrix3":
        return self


# --------------------------------------------------------------------- objects
class MockObject:
    """A hostile MAXScript value: class tag + property bag + failure arms.

    Reads/writes of bag properties route through the runtime (chaos) and the
    per-object arms, and writes land in ``rt.mutation_log``. A stale object
    (deleted node handle) raises ``MockRtError`` on *any* attribute access —
    including reads of ``name``.
    """

    def __init__(self, rt: "FakeMaxRuntime", cls: str = "MAXObject",
                 props: Optional[Dict[str, Any]] = None, superclass: Any = "MAXObject"):
        object.__setattr__(self, "_mg", {
            "rt": rt, "cls": cls, "superclass": superclass,
            "props": dict(props or {}),
            "stale": False, "get_arms": {}, "set_arms": {},
        })

    # ------------------------------------------------------------ hostility controls
    def set_stale(self, on: bool = True) -> "MockObject":
        self._mg["stale"] = bool(on)
        return self

    @property
    def stale(self) -> bool:
        return bool(self._mg["stale"])

    def arm_get(self, prop: str, exc: Optional[Exception] = None) -> "MockObject":
        self._mg["get_arms"][prop] = exc or MockRtError(
            "armed get failure on .{0} (locked/broken property)".format(prop))
        return self

    def arm_set(self, prop: str, exc: Optional[Exception] = None) -> "MockObject":
        self._mg["set_arms"][prop] = exc or MockRtError(
            "armed set failure on .{0} (read-only/locked property)".format(prop))
        return self

    def disarm(self, prop: Optional[str] = None) -> "MockObject":
        if prop is None:
            self._mg["get_arms"].clear()
            self._mg["set_arms"].clear()
        else:
            self._mg["get_arms"].pop(prop, None)
            self._mg["set_arms"].pop(prop, None)
        return self

    def set_undefined(self, prop: str) -> "MockObject":
        """The property EXISTS (isProperty True) but reads back ``undefined``."""
        self._mg["props"][prop] = UNDEFINED
        return self

    def get_raw(self, prop: str, default: Any = None) -> Any:
        """Bypass arms/chaos — test-side introspection of the property bag."""
        return self._mg["props"].get(prop, default)

    # ------------------------------------------------------------ attribute protocol
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        mg = object.__getattribute__(self, "_mg")
        if mg["stale"]:
            raise MockRtError("stale handle: read of .{0} on a deleted node".format(name))
        rt = mg["rt"]
        rt._touch("get .{0}".format(name), kind="get")
        arm = mg["get_arms"].get(name)
        if arm is not None:
            raise arm
        props = mg["props"]
        if name in props:
            return props[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        mg = object.__getattribute__(self, "_mg")
        if mg["stale"]:
            raise MockRtError("stale handle: write of .{0} on a deleted node".format(name))
        rt = mg["rt"]
        rt._touch("set .{0}".format(name), kind="set")
        arm = mg["set_arms"].get(name)
        if arm is not None:
            raise arm
        old = mg["props"].get(name, UNDEFINED)
        mg["props"][name] = value
        rt.mutation_log.append(("set", self, name, old, value))

    def __repr__(self) -> str:
        try:
            mg = object.__getattribute__(self, "_mg")
            label = mg["props"].get("name", "")
            return "<MockObject {0} {1!r}{2}>".format(
                mg["cls"], label, " STALE" if mg["stale"] else "")
        except Exception:
            return "<MockObject ?>"


class MockNode(MockObject):
    """A scene node: name + layer + pos + transform, all as hostile properties."""

    def __init__(self, rt: "FakeMaxRuntime", cls: str, name: str,
                 props: Optional[Dict[str, Any]] = None, layer: str = "0",
                 pos=(0.0, 0.0, 0.0), transform: Optional[MockMatrix3] = None):
        bag: Dict[str, Any] = {
            "name": name,
            "layer": MockObject(rt, "Layer", {"name": layer}),
            "pos": MockPoint3(*pos),
            "transform": transform or MockMatrix3(rt),
        }
        bag.update(props or {})
        super().__init__(rt, cls, bag, superclass="Node")
        rt._nodes.append(self)


class MockCollection(list):
    """``rt.cameras`` / ``rt.lights`` — can fail at iteration start (chaos) or
    break mid-iteration (``fail_after`` yields), like a corrupted scene array."""

    def __init__(self, rt: "FakeMaxRuntime", what: str):
        super().__init__()
        self._rt = rt
        self._what = what
        self.fail_after: Optional[int] = None

    def __iter__(self):
        # NOTE: list(self) would re-enter THIS generator (list subclasses honor
        # an overridden __iter__) — iterate the base class directly instead.
        self._rt._touch("iterate {0}".format(self._what), kind="rt")
        count = 0
        for item in list.__iter__(self):
            yield item
            count += 1
            if self.fail_after is not None and count >= self.fail_after:
                raise MockRtError("{0} collection broke mid-iteration".format(self._what))


class MockLayer(MockObject):
    def __init__(self, rt: "FakeMaxRuntime", name: str):
        super().__init__(rt, "Layer", {"name": name})
        self.nodes: List[MockObject] = []

    def addNode(self, node: MockObject) -> None:
        self._mg["rt"]._touch("layer.addNode", kind="rt")
        self.nodes.append(node)
        self._mg["rt"].mutation_log.append(("layer_add", self, node))


class MockLayerManager(MockObject):
    def __init__(self, rt: "FakeMaxRuntime"):
        super().__init__(rt, "LayerManager")
        self._layers: Dict[str, MockLayer] = {}

    def getLayerFromName(self, name: str):
        self._mg["rt"]._touch("LayerManager.getLayerFromName", kind="rt")
        return self._layers.get(name)

    def newLayerFromName(self, name: str) -> MockLayer:
        self._mg["rt"]._touch("LayerManager.newLayerFromName", kind="rt")
        layer = MockLayer(self._mg["rt"], name)
        self._layers[name] = layer
        return layer


class MockViewport(MockObject):
    def __init__(self, rt: "FakeMaxRuntime"):
        super().__init__(rt, "viewport")
        self._camera: Any = None

    def getCamera(self):
        self._mg["rt"]._touch("viewport.getCamera", kind="rt")
        return self._camera

    def setCamera(self, cam) -> None:
        self._mg["rt"]._touch("viewport.setCamera", kind="rt")
        self._camera = cam
        self._mg["rt"].mutation_log.append(("viewport_setCamera", cam))


class MockRenderSceneDialog(MockObject):
    def close(self) -> None:
        self._mg["rt"]._touch("renderSceneDialog.close", kind="rt")
        self._mg["rt"].mutation_log.append(("renderSceneDialog_close",))


class MockActionMan(MockObject):
    def __init__(self, rt: "FakeMaxRuntime"):
        super().__init__(rt, "actionMan", {"numActionTables": 0})
        self._tables: List[Any] = []

    def getActionTable(self, index: int):
        self._mg["rt"]._touch("actionMan.getActionTable", kind="rt")
        return self._tables[index - 1]


# --------------------------------------------------------------------- undo
class _UndoCM:
    """``pymxs.undo(True, "label")`` — records enter/exit, NEVER swallows
    exceptions (real pymxs.undo propagates; it is not a rollback)."""

    def __init__(self, rt: "FakeMaxRuntime", label: str):
        self._rt = rt
        self._label = label

    def __enter__(self) -> "_UndoCM":
        self._rt.mutation_log.append(("undo_enter", self._label))
        self._rt._undo_depth += 1
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._rt._undo_depth -= 1
        self._rt.mutation_log.append(
            ("undo_exit", self._label, "ok" if exc_type is None else "raise"))
        return False


# --------------------------------------------------------------------- runtime
def _managed_global(name: str):
    """A runtime global (environmentMap, renderWidth …) with arms + chaos + log."""
    def getter(self: "FakeMaxRuntime"):
        self._touch("global {0}".format(name), kind="rt")
        arm = self._global_arms.get(name)
        if arm is not None:
            raise arm
        return self._globals.get(name)

    def setter(self: "FakeMaxRuntime", value):
        self._touch("set global {0}".format(name), kind="rt")
        arm = self._global_arms.get(name)
        if arm is not None:
            raise arm
        old = self._globals.get(name)
        self._globals[name] = value
        self.mutation_log.append(("set_global", name, old, value))

    return property(getter, setter)


class FakeMaxRuntime:
    """The hostile ``pymxs.runtime`` stand-in. See module docstring for design."""

    Color = MockColor
    Point3 = MockPoint3
    textureMap = MockTextureMap

    environmentMap = _managed_global("environmentMap")
    maxFilePath = _managed_global("maxFilePath")
    maxFileName = _managed_global("maxFileName")
    renderWidth = _managed_global("renderWidth")
    renderHeight = _managed_global("renderHeight")

    def __init__(self, seed: int = CHAOS_SEED):
        self.rng = random.Random(seed)
        self.seed = seed
        #: chaos probabilities (0 = off); deterministic for a fixed seed
        self.chaos_rt = 0.0
        self.chaos_set = 0.0
        self.chaos_get = 0.0

        self.mutation_log: List[tuple] = []
        self.undefined = UNDEFINED

        self.cameras = MockCollection(self, "cameras")
        self.lights = MockCollection(self, "lights")
        self.objects = MockObject(self, "ObjectSet", {"count": 0})
        self.currentTime = MockObject(self, "time", {"frame": 1})

        self.SceneExposureControl = MockObject(self, "SceneExposureControl",
                                               {"exposureControl": None})
        self.renderers = MockObject(self, "renderers", {"current": None})
        self.viewport = MockViewport(self)
        self.LayerManager = MockLayerManager(self)
        self.renderSceneDialog = MockRenderSceneDialog(self, "renderSceneDialog")
        self.actionMan = MockActionMan(self)

        self._nodes: List[MockObject] = []          # every MockNode ever made
        self._classes: Dict[int, Any] = {}          # id(obj) → class identity
        self._superclasses: Dict[int, Any] = {}
        self._globals: Dict[str, Any] = {
            "environmentMap": None, "maxFilePath": "", "maxFileName": "",
            "renderWidth": 1920, "renderHeight": 1080,
        }
        self._global_arms: Dict[str, Exception] = {}
        self._rt_arms: Dict[str, Exception] = {}    # named rt-method arms
        self._makers: Dict[str, Callable] = {}      # class/function globals
        self._maker_arms: Dict[str, Exception] = {}
        self._execute_map: Dict[str, Any] = {}
        self._undo_depth = 0
        self._register_default_makers()

    # ------------------------------------------------------------ chaos core
    def _touch(self, what: str, kind: str = "rt") -> None:
        """One hostile-runtime operation. Raises MockRtError when the seeded
        chaos roll for this channel hits. kind ∈ rt | set | get."""
        prob = {"rt": self.chaos_rt, "set": self.chaos_set,
                "get": self.chaos_get}.get(kind, 0.0)
        if prob > 0.0 and self.rng.random() < prob:
            raise MockRtError("chaos failure (seed {0}): {1}".format(self.seed, what))

    def reseed(self, seed: Optional[int] = None) -> None:
        self.rng.seed(self.seed if seed is None else seed)

    def reset_log(self) -> None:
        del self.mutation_log[:]

    # ------------------------------------------------------------ registry
    def register(self, obj: Any, cls: Any, superclass: Any = None) -> Any:
        self._classes[id(obj)] = cls
        if superclass is not None:
            self._superclasses[id(obj)] = superclass
        return obj

    def classOf(self, obj: Any) -> Any:
        self._touch("classOf", kind="rt")
        if isinstance(obj, MockObject) and obj.stale:
            raise MockRtError("classOf on a stale node handle")
        if obj is UNDEFINED or obj is None:
            return "UndefinedClass"
        hit = self._classes.get(id(obj))
        if hit is not None:
            return hit
        if isinstance(obj, MockObject):
            return obj._mg["cls"]
        if isinstance(obj, MockColor):
            return MockColor
        if isinstance(obj, (MockPoint3, MockPoint2)):
            return type(obj)
        return type(obj).__name__

    def superClassOf(self, obj: Any) -> Any:
        self._touch("superClassOf", kind="rt")
        if isinstance(obj, MockObject) and obj.stale:
            raise MockRtError("superClassOf on a stale node handle")
        hit = self._superclasses.get(id(obj))
        if hit is not None:
            return hit
        if isinstance(obj, MockObject):
            return obj._mg["superclass"]
        return type(obj).__name__

    def isProperty(self, obj: Any, name: Any) -> bool:
        self._touch("isProperty", kind="rt")
        if isinstance(obj, MockObject):
            if obj.stale:
                raise MockRtError("isProperty on a stale node handle")
            return str(name) in obj._mg["props"]
        try:
            return hasattr(obj, str(name))
        except Exception:
            return False

    def isValidNode(self, obj: Any) -> bool:
        self._touch("isValidNode", kind="rt")
        return isinstance(obj, MockNode) and not obj.stale

    def getNodeByName(self, name: str, exact: bool = False):
        self._touch("getNodeByName", kind="rt")
        arm = self._rt_arms.get("getNodeByName")
        if arm is not None:
            raise arm
        for pool in (self.cameras, self.lights, self._nodes):
            for node in pool:
                try:
                    if isinstance(node, MockObject) and not node.stale \
                            and node.get_raw("name") == name:
                        return node
                except Exception:
                    continue
        return None

    def getPropNames(self, obj: Any) -> List[str]:
        self._touch("getPropNames", kind="rt")
        if isinstance(obj, MockObject):
            if obj.stale:
                raise MockRtError("getPropNames on a stale node handle")
            return list(obj._mg["props"].keys())
        return []

    def Name(self, n: Any) -> str:
        return str(n)                       # value constructor — never chaos-fails

    def delete(self, obj: Any) -> None:
        self._touch("delete", kind="rt")
        self.mutation_log.append(("delete", obj))
        if isinstance(obj, MockObject):
            obj.set_stale(True)             # deleted nodes become stale handles

    # ------------------------------------------------------------ maxscript bridge
    def execute(self, expr: str):
        self._touch("execute", kind="rt")
        self.mutation_log.append(("execute", expr))
        outcome = self._execute_map.get(expr)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome                      # None default, like a void maxscript call

    def arm_execute(self, expr: str, outcome: Any) -> None:
        self._execute_map[expr] = outcome

    def undo_context(self, *args, **kwargs) -> _UndoCM:
        label = str(args[1]) if len(args) > 1 else str(kwargs.get("label", ""))
        return _UndoCM(self, label)

    def undo(self, *args, **kwargs) -> None:
        self._touch("rt.undo", kind="rt")
        self.mutation_log.append(("undo_rt",))

    def redo(self, *args, **kwargs) -> None:
        self._touch("rt.redo", kind="rt")
        self.mutation_log.append(("redo_rt",))

    # ------------------------------------------------------------ viewport / misc
    def redrawViews(self) -> None:
        self._touch("redrawViews", kind="rt")
        self.mutation_log.append(("redrawViews",))

    def quatToEuler(self, q: Any):
        return types.SimpleNamespace(x=0.0, y=0.0, z=0.0)

    def transMatrix(self, p: MockPoint3) -> MockMatrix3:
        return MockMatrix3(self, translation=p)

    def rotateZMatrix(self, degrees_: float) -> MockMatrix3:
        return MockMatrix3(self)

    def Point2(self, x: float, y: float) -> MockPoint2:
        return MockPoint2(x, y)

    def color(self, r: float, g: float, b: float) -> MockColor:
        return MockColor(r, g, b)

    # ------------------------------------------------------------ render / bitmap I/O
    def render(self, **kwargs):
        self._touch("render", kind="rt")
        arm = self._rt_arms.get("render")
        if arm is not None:
            raise arm
        self.mutation_log.append(("render", dict(kwargs)))
        return MockObject(self, "Bitmap", {"width": kwargs.get("outputwidth", 640),
                                           "height": kwargs.get("outputheight", 480),
                                           "filename": ""})

    def save(self, bm: Any) -> None:
        self._touch("bitmap save", kind="rt")
        self.mutation_log.append(("save", bm))      # writes no real file by default

    def close(self, bm: Any) -> None:
        self._touch("bitmap close", kind="rt")
        self.mutation_log.append(("close", bm))

    def copy(self, src: Any, dst: Any) -> None:
        self._touch("bitmap copy", kind="rt")
        self.mutation_log.append(("copy_bitmap", src, dst))

    # ------------------------------------------------------------ makers
    def _register_default_makers(self) -> None:
        def _texmap(cls):
            def make():
                self._touch("make " + cls, kind="rt")
                tex = MockObject(self, cls, {"HDRIMapName": "", "horizontalRotation": 0.0},
                                 superclass=MockTextureMap)
                self.mutation_log.append(("create", cls, tex))
                return tex
            return make

        def _node(cls, props):
            def make():
                self._touch("make " + cls, kind="rt")
                node = MockNode(self, cls, cls + "_auto", props=dict(props))
                self.mutation_log.append(("create", cls, node))
                return node
            return make

        def _exposure_control():
            self._touch("vrayCreateVRayExposureControl", kind="rt")
            ec = MockObject(self, "VRayExposureControl",
                            {"ev": 0.0, "mode": 1, "temperature": 6500.0,
                             "whitebalance": None, "whitebalance_mode": 0})
            self.mutation_log.append(("create", "VRayExposureControl", ec))
            return ec

        self._makers.update({
            "VRayBitmap": _texmap("VRayBitmap"),
            "VRayHDRI": _texmap("VRayHDRI"),
            "VRayLight": _node("VRayLight", {"type": 0, "enabled": True,
                                             "multiplier": 1.0}),
            "VRaySun": _node("VRaySun", {"enabled": True, "intensity_multiplier": 1.0,
                                         "size_multiplier": 1.0, "turbidity": 3.0,
                                         "target": None}),
            "VRayIES": _node("VRayIES", {"enabled": True, "multiplier": 1.0}),
            "Targetobject": _node("Targetobject", {}),
            "vrayCreateVRayExposureControl": _exposure_control,
        })

    def add_maker(self, name: str, factory: Callable) -> None:
        self._makers[name] = factory

    def remove_maker(self, name: str) -> None:
        """Simulate 'missing V-Ray': the class global does not exist at all."""
        self._makers.pop(name, None)

    def arm_maker(self, name: str, exc: Optional[Exception] = None) -> None:
        self._maker_arms[name] = exc or MockRtError(
            "armed maker failure: {0} (plugin class broken)".format(name))

    def arm_global(self, name: str, exc: Optional[Exception] = None) -> None:
        self._global_arms[name] = exc or MockRtError(
            "armed global failure: {0}".format(name))

    def arm_rt(self, what: str, exc: Optional[Exception] = None) -> None:
        self._rt_arms[what] = exc or MockRtError("armed rt failure: {0}".format(what))

    def __getattr__(self, name: str) -> Any:
        # only fires for attributes NOT found normally — serves class/function
        # globals (VRayBitmap, vrayCreateVRayExposureControl, vrayExportVRScene…);
        # an absent maker raises AttributeError, exactly like a missing plugin.
        makers = object.__getattribute__(self, "_makers")
        if name in makers:
            arms = object.__getattribute__(self, "_maker_arms")
            arm = arms.get(name)
            if arm is not None:
                def failing(*args, **kwargs):
                    raise arm
                return failing
            return makers[name]
        raise AttributeError(name)

    # ------------------------------------------------------------ log queries
    def sets(self, obj: Any = None, prop: Optional[str] = None) -> List[tuple]:
        return [e for e in self.mutation_log
                if e[0] == "set"
                and (obj is None or e[1] is obj)
                and (prop is None or e[2] == prop)]

    def created(self, cls: Optional[str] = None) -> List[tuple]:
        return [e for e in self.mutation_log
                if e[0] == "create" and (cls is None or e[1] == cls)]

    def deletes(self, obj: Any = None) -> List[tuple]:
        return [e for e in self.mutation_log
                if e[0] == "delete" and (obj is None or e[1] is obj)]

    def ec_assignments(self) -> List[tuple]:
        return self.sets(self.SceneExposureControl, "exposureControl")


# --------------------------------------------------------------------- install
def install(rt: FakeMaxRuntime) -> types.ModuleType:
    """Build the fake ``pymxs`` module around ``rt`` (caller injects it into
    ``sys.modules`` — use ``monkeypatch.setitem`` so the process stays clean)."""
    mod = types.ModuleType("pymxs")
    mod.runtime = rt
    mod.undo = rt.undo_context
    return mod
