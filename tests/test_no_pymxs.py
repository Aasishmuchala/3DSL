"""Architecture guard — core/ must stay pure so the whole engine tests off-Max.

Same enforcement as MaxDirector: pymxs and Qt may only appear in maxbridge/ and ui/.
"""

import importlib
import pathlib

CORE = pathlib.Path(__file__).parent.parent / "maxgaffer" / "core"
FORBIDDEN = ("import pymxs", "from pymxs", "import PySide", "from PySide",
             "import MaxPlus", "from MaxPlus")


def test_core_source_never_touches_max_or_qt():
    offenders = []
    for path in CORE.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, offenders


def test_every_core_module_imports_cleanly():
    for path in sorted(CORE.glob("*.py")):
        mod = "maxgaffer.core." + path.stem if path.stem != "__init__" else "maxgaffer.core"
        importlib.import_module(mod)


def test_prompts_carry_the_contract():
    """The hard rules the loop depends on must actually be in the prompts."""
    from maxgaffer.core import prompts

    assert "ONLY a JSON object" in prompts.ANALYZE_SYSTEM
    assert "AT MOST 4" in prompts.DELTAS_SYSTEM
    assert "LOCKED" in prompts.DELTAS_SYSTEM
    assert "ANALYTIC" in prompts.DELTAS_SYSTEM
    assert "stop" in prompts.DELTAS_SYSTEM
    assert "best_index" in prompts.SWEEP_SYSTEM
    txt = prompts.deltas_user_text("TABLE", {"a": 1}, [(0, 50.0)], {"exposure.ev": 11.0},
                                   iteration=1, max_iterations=5)
    assert "TABLE" in txt and "iter0=50.0" in txt and "exposure.ev" in txt
    assert "judge visually" in prompts.deltas_user_text("T", {}, [], {}, 0, 5)
