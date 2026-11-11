"""Run: python test_paths.py  (no Blender needed: bpy is stubbed)."""
import importlib.util
import os
import sys
import tempfile
import types

bpy = types.ModuleType("bpy")
bpy.app = types.SimpleNamespace(version_string="0", tempdir="")
bpy.data = types.SimpleNamespace(filepath="", is_saved=False)
bpy_types = types.ModuleType("bpy.types")
for _n in ("Menu", "Operator", "Panel", "PropertyGroup"):
    setattr(bpy_types, _n, type(_n, (), {}))
bpy_props = types.ModuleType("bpy.props")
for _n in ("EnumProperty", "BoolProperty", "IntProperty", "FloatProperty",
           "FloatVectorProperty", "StringProperty", "PointerProperty",
           "CollectionProperty"):
    setattr(bpy_props, _n, lambda *a, **k: None)
bpy.types, bpy.props = bpy_types, bpy_props
sys.modules.update({"bpy": bpy, "bpy.types": bpy_types, "bpy.props": bpy_props})

_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playblast_studio.py")
_spec = importlib.util.spec_from_file_location("playblast_studio", _path)
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)   # also runs the BUILTIN_PRESETS key assert


def _touch(folder, *names):
    for n in names:
        open(os.path.join(folder, n), "w").close()


def test_base_stem_of():
    assert ps.base_stem_of("clip_v003") == "clip"
    assert ps.base_stem_of("clip") == "clip"


def test_clean_name_path_stays_inside_folder():
    assert ps._clean_name_path("../a//b/./c") == "a/b/c"
    assert ps._clean_name_path("/x") == "x"


def test_scan_existing_versions():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "clip_v001.mp4", "clip_v100.mp4", "clip_v002_0001.png",
               "other_v999.mp4", "clip_notes.txt")
        assert ps.scan_existing_versions(d, "clip") == 100
        assert ps.scan_existing_versions(d, "nothing") == 0


def test_find_output_files_ignores_unrelated_files():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "shot_0001.png", "shot_0002.png", "shot_notes.txt")
        found = ps.find_output_files(os.path.join(d, "shot_"), False)
        assert sorted(os.path.basename(p) for p in found) == \
            ["shot_0001.png", "shot_0002.png"]
        _touch(d, "mov.mp4", "mov.txt")
        found = ps.find_output_files(os.path.join(d, "mov"), True)
        assert [os.path.basename(p) for p in found] == ["mov.mp4"]


def test_presets_reset_scale():
    assert ps.BUILTIN_PRESETS["Review"]["resolution_percentage"] == 100
    assert ps.BUILTIN_PRESETS["Draft"]["resolution_percentage"] == 50


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_"):
            _fn()
            print("ok", _name)
