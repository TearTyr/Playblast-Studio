bl_info = {
    "name": "Playblast Animation Plugin",
    "author": "TearTyr",
    "version": (4, 5, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Playblast",
    "description": (
        "Studio-style viewport playblasts: one-click review renders with custom "
        "resolutions, codecs, audio, burn-in stamps, naming tokens, versioning, "
        "presets and viewport display control. Settings live in the Adjust Last "
        "Operation panel (viewport bottom-left). Targets Blender 4.0 and newer."
    ),
    "warning": "",
    "doc_url": "https://github.com/TearTyr/Playblast-Studio",
    "tracker_url": "https://github.com/TearTyr/Playblast-Studio/issues",
    "category": "Animation",
}

import bpy
import json
import logging
import os
import re
import tempfile
import time
from bpy.types import Menu, Operator, Panel, PropertyGroup
from bpy.props import (
    EnumProperty,
    BoolProperty,
    IntProperty,
    FloatProperty,
    FloatVectorProperty,
    StringProperty,
    PointerProperty,
    CollectionProperty,
)


# =========================================================================
#  Logging & reporting
# =========================================================================

LOG = logging.getLogger("playblast")
if not LOG.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[playblast] %(levelname)s %(message)s"))
    LOG.addHandler(_handler)
LOG.setLevel(logging.INFO)
LOG.propagate = False


def report(operator, level, message):
    """Logs `message` and pushes it to the operator's report list."""
    if level == 'ERROR':
        LOG.error(message)
    elif level == 'WARNING':
        LOG.warning(message)
    else:
        LOG.info(message)
    try:
        operator.report({level}, message)
    except Exception:
        pass


# =========================================================================
#  Platform helpers
# =========================================================================

def get_default_temp_dir():
    """Returns a safe, cross-platform temporary directory."""
    tempdir = getattr(bpy.app, "tempdir", "") or ""
    if tempdir:
        return tempdir
    try:
        return tempfile.gettempdir() + os.sep
    except Exception:
        return ""


# =========================================================================
#  RNA-safe property access
#
#  Enum items and even whole properties differ between Blender releases (and
#  between builds, e.g. FFmpeg compiled without x264).  Nothing is assumed:
#  every write is validated against the live RNA of the running build.
# =========================================================================

def enum_identifiers(rna_owner, prop_name):
    """
    Legal identifiers for an enum property, as reported by *this* build.

    `rna_owner` must be an RNA instance so that context dependent enums - such
    as `file_format`, filtered by `media_type` in 5.x - resolve correctly.
    Returns an empty set when unknown; callers treat that as "try anyway".
    """
    try:
        prop = rna_owner.bl_rna.properties.get(prop_name)
        if prop is None or getattr(prop, "type", None) != 'ENUM':
            return set()
        return {item.identifier for item in prop.items}
    except Exception:
        return set()


def assign_enum(rna_owner, prop_name, candidates):
    """
    Assigns the first candidate this Blender accepts.  Returns the identifier
    applied, or None.  Never raises.
    """
    if isinstance(candidates, str):
        candidates = (candidates,)
    valid = enum_identifiers(rna_owner, prop_name)
    for candidate in candidates:
        if not candidate:
            continue
        if valid and candidate not in valid:
            continue
        try:
            setattr(rna_owner, prop_name, candidate)
            return candidate
        except Exception:
            continue
    return None


def set_attr_safe(rna_owner, prop_name, value):
    """Sets any property, swallowing version/permission errors. Returns bool."""
    try:
        setattr(rna_owner, prop_name, value)
        return True
    except Exception as exc:
        # Silent failures hid the rv3d.persp bug for a whole release: leave a
        # trace at debug level so 'why did nothing happen' is answerable.
        LOG.debug("Cannot set %s: %s", prop_name, exc)
        return False


def get_attr_safe(rna_owner, prop_name, default=None):
    """Reads any property, returning `default` when unavailable."""
    try:
        return getattr(rna_owner, prop_name)
    except Exception:
        return default


# =========================================================================
#  Output path helpers
# =========================================================================

VIDEO_EXTENSIONS = (
    '.mov', '.mp4', '.avi', '.ogv', '.ogg', '.mkv', '.webm', '.mpg', '.mpeg',
    '.dv', '.flv',
)
# First extension Blender accepts per FFmpeg container.  A movie path that
# already ends in one of these is used VERBATIM; without it Blender appends
# the frame range ('clip0001-0250.mp4').
CONTAINER_EXTENSIONS = {
    'MPEG4': '.mp4', 'QUICKTIME': '.mov', 'AVI': '.avi',
    'OGG': '.ogv', 'MKV': '.mkv', 'WEBM': '.webm',
}
# Extension Blender writes per image format (used for the live name preview).
IMAGE_FORMAT_EXTENSIONS = {
    'PNG': '.png', 'JPEG': '.jpg', 'JPEG2000': '.jp2', 'OPEN_EXR': '.exr',
    'CINEON': '.cin', 'DPX': '.dpx', 'BMP': '.bmp', 'TARGA': '.tga',
    'TIFF': '.tif',
}
IMAGE_EXTENSIONS = (
    '.png', '.jpg', '.jpeg', '.jp2', '.j2k', '.cin', '.dpx', '.bmp', '.tga',
    '.tif', '.tiff', '.exr', '.webp', '.avif', '.rgb', '.hdr',
)

# Illegal in Windows file names; safest to reject everywhere.
_ILLEGAL_NAME_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')

# name_v003 (anchored at the END: a version suffix is only stripped when it
# really is the last component of the stem).
_VERSION_SUFFIX_RE = re.compile(r'_v(\d{1,6})$')


def sanitize_filename(name):
    """Removes characters that would make the file unwritable on some OS."""
    name = _ILLEGAL_NAME_CHARS.sub("_", str(name or "").strip())
    return name.rstrip(". ")


def movie_filepath(render, base_path):
    """
    `base_path` + the extension of the container Blender will really use
    (read after the format is applied, so fallbacks are honoured).  Handing
    this to `render.filepath` gives a clean 'name.mp4' instead of Blender's
    'name0000-0042.mp4'.
    """
    container = get_attr_safe(get_attr_safe(render, "ffmpeg"), "format")
    return base_path + CONTAINER_EXTENSIONS.get(container, ".mp4")


def strip_redundant_extension(name, is_video):
    """Drops a trailing extension Blender appends anyway (use_file_extension)."""
    lowered = name.lower()
    for ext in (VIDEO_EXTENSIONS if is_video else IMAGE_EXTENSIONS):
        if lowered.endswith(ext) and len(name) > len(ext):
            return name[:-len(ext)]
    return name


def parse_resolution(template_str):
    """Extracts resolution integers from strings like 'x1920y1080'."""
    match = re.search(r"x(\d+)y(\d+)", template_str or "")
    if match:
        return int(match.group(1)), int(match.group(2))
    return 1920, 1080


def output_is_video(settings):
    """
    True when the playblast will be written as a movie file.

    Mirrors what the operator actually does: with "Save to file" off the output
    is always a temporary PNG sequence, whatever container the UI shows.
    """
    return bool(settings.saveOutputToFile) and settings.outputEncoding != 'IMAGE'


def directory_is_writable(directory):
    """Cheap, I/O-free test - safe to call from a panel draw."""
    try:
        return bool(directory) and os.path.isdir(directory) \
            and os.access(directory, os.W_OK | os.X_OK)
    except Exception:
        return False


def directory_write_probe(directory):
    """Really writes and removes a probe file. Called once per playblast."""
    if not directory_is_writable(directory):
        return False
    probe = os.path.join(directory, ".playblast_write_test")
    try:
        with open(probe, "w") as handle:
            handle.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def find_output_files(base_path, is_video):
    """
    Files Blender actually wrote for `base_path` (which has no extension).
    Used to confirm a playblast succeeded and to report the real file name.
    Movies: 'name.mp4' (a legacy 'name0000-0042.mp4' is accepted too).
    Sequences: 'name<frame>.<ext>'.
    """
    directory = os.path.dirname(base_path) or "."
    stem = os.path.basename(base_path)
    try:
        entries = os.listdir(directory) if stem else []
    except OSError:
        return []

    if is_video:
        movie = re.compile(r'^%s(?:\d{4,}-\d{4,})?(\.[^.]+)$' % re.escape(stem))
        names = [e for e in entries if movie.match(e)
                 and movie.match(e).group(1).lower() in VIDEO_EXTENSIONS]
    else:
        names = [e for e in entries if e.startswith(stem) and "." in e[len(stem):]]

    paths = [os.path.join(directory, n) for n in names]
    paths = [p for p in paths if os.path.isfile(p)]
    return sorted(paths, key=os.path.getmtime)


# =========================================================================
#  File name tokens & auto-versioning
# =========================================================================

# The Adjust Last Operation panel is narrow: the token list is drawn as short
# lines there, and joined into one string for tooltips.
TOKEN_HELP_LINES = (
    "{scene} {camera} {blend} {user}",
    "{date} {time} {datetime}",
    "{start} {end} {frames} {fps} {res}",
)
TOKEN_HELP = " ".join(TOKEN_HELP_LINES)

def get_user_name():
    """Login name, without ever raising."""
    try:
        import getpass
        return getpass.getuser() or "user"
    except Exception:
        return os.environ.get("USER") or os.environ.get("USERNAME") or "user"


_WILD = '\ue000'  # stands for 'any number' when scanning for versions
_TOKEN_RE = re.compile(r'\{(\w+)\}')


def _token_value(value):
    """
    A token value is ONE path component.  Without this a camera named 'A/B'
    would silently create a sub-folder, and a scene named '..' could climb out
    of the output folder.
    """
    return sanitize_filename(str(value)).replace("/", "-").replace("\\", "-")


def _clean_name_path(name):
    """
    The file-name template may contain sub-folders ('shots/{scene}'), but never
    empty, '.' or '..' segments, so the result always stays under the folder.
    """
    parts = [part for part in re.split(r'[\\/]+', name)
             if part and part not in ('.', '..')]
    return "/".join(parts)


def expand_name_tokens(template, scene, settings, wild=False):
    """
    Replaces {tokens} in the output file name.

    Unknown tokens are left untouched so a typo is visible in the file name
    rather than silently swallowed.  Versioning is handled separately by
    `autoVersion`, so it works with or without tokens.
    {start} {end} {frames} follow the range that will really be rendered.
    """
    if not template:
        return "playblast"

    render = scene.render
    camera = getattr(scene, "camera", None)
    start, end, step = effective_frame_range(settings, scene)

    blend_path = getattr(bpy.data, "filepath", "") or ""
    now = time.localtime()   # one clock read: date/time/datetime always agree
    values = {
        'scene': scene.name or "scene",
        'camera': (camera.name if camera is not None else "nocam"),
        'blend': (os.path.splitext(os.path.basename(blend_path))[0] or "untitled"),
        'user': get_user_name(),
        'date': time.strftime("%Y-%m-%d", now),
        'time': time.strftime("%H%M%S", now),
        'datetime': time.strftime("%Y%m%d_%H%M%S", now),
        # With wild=True the range tokens match ANY range, so a changed frame
        # range no longer restarts the version count at v001.
        'start': _WILD if wild else str(start),
        'end': _WILD if wild else str(end),
        'frames': _WILD if wild else str(frame_count(start, end, step)),
        'fps': "%g" % scene_fps(scene),
        'res': "%dx%d" % (int(render.resolution_x), int(render.resolution_y)),
    }

    values = {key: _token_value(value) for key, value in values.items()}
    # ONE pass: a value that itself contains '{user}' (a scene called
    # 'shot{user}') must not be expanded a second time.  Unknown tokens stay.
    out = _TOKEN_RE.sub(
        lambda m: values[m.group(1)] if m.group(1) in values else m.group(0),
        template)
    # Collapse the separators a blank token can leave behind.
    out = re.sub(r'_{2,}', '_', out).strip("_ ")
    return out or "playblast"


def scan_existing_versions(directory, base_stem):
    """
    Highest existing `_v###` for `base_stem` in `directory` (0 if none).

    A version is always terminated by an underscore (sequences:
    'clip_v001_0001.png'), a dot (movies: 'clip_v001.mp4') or the end of the
    name, so any digit count parses cleanly: v100, v1000, v12345.  Movies
    from older versions ('clip_v0010000-0042.mp4') are counted as well.
    """
    if not directory or not os.path.isdir(directory):
        return 0
    prefix = re.escape(base_stem.rstrip('_')).replace(_WILD, r'-?\d+')
    pattern = re.compile(r'^%s_v(\d+)(?:_|\.|$|\d{4,}-\d{4,}\.)' % prefix)
    highest = 0
    try:
        for entry in os.listdir(directory):
            match = pattern.match(entry)
            if match:
                highest = max(highest, int(match.group(1)))
    except OSError:
        pass
    return highest


def base_stem_of(stem):
    """'clip_v003' -> 'clip' ; 'clip' -> 'clip'."""
    stem = stem or ""
    match = _VERSION_SUFFIX_RE.search(stem)
    return stem[:match.start()] if match else stem


def _scene_or_current(scene):
    if scene is not None:
        return scene
    try:
        return bpy.context.scene
    except Exception:
        return None


def _output_directory(settings):
    """
    Absolute output folder.  With 'Save to File' off the output is a throwaway
    sequence and always goes to the temp dir.  '//' paths need a saved .blend,
    else temp.
    """
    if not settings.saveOutputToFile:
        return get_default_temp_dir() or os.getcwd()
    base = (settings.outputFilePath or "").strip() or get_default_temp_dir()
    if base.startswith("//") and not bpy.data.is_saved:
        base = get_default_temp_dir()
    else:
        try:
            base = bpy.path.abspath(base)
        except Exception:
            base = os.path.abspath(base)
    return os.path.expanduser(base)


def output_directory_notice(settings):
    """
    Why the output folder differs from what the user entered ('' when it does
    not).  I/O-free, so it is safe to call from a draw method.  Blender deletes
    its session temp folder on exit, so a silent fallback would lose the file.
    """
    if not settings.saveOutputToFile:
        return ""
    base = (settings.outputFilePath or "").strip()
    if not base:
        return "No output folder set: using Blender's temp folder."
    if base.startswith("//") and not bpy.data.is_saved:
        return "Unsaved .blend: output goes to Blender's temp folder."
    return ""


def _output_stem(settings, scene, version=0, wild=False):
    """File name without folder or extension, versioned when asked."""
    if not settings.saveOutputToFile:
        return "playblast_temp_"
    is_video = output_is_video(settings)
    name = settings.outputFileName or "playblast"
    if settings.useNameTokens and scene is not None:
        name = expand_name_tokens(name, scene, settings, wild)
    # Keeps sub-folders, drops leading separators ('/x' would make
    # os.path.join discard the output folder) and '..' segments.
    name = _clean_name_path(sanitize_filename(name))
    name = strip_redundant_extension(name, is_video) or "playblast"
    if settings.autoVersion and version > 0:
        name = "%s_v%0*d" % (base_stem_of(name),
                             int(settings.versionPadding), int(version))
    # Sequences get a separator before the frame number so the version digits
    # stay unambiguous ('name_v001_0001.png').
    return name if is_video else name + "_"


def resolve_render_filepath(settings, scene=None, version=0, wild=False):
    """
    Absolute output path WITHOUT extension (Blender appends it).  Touches no
    files, so it is safe to call while drawing.  `outputFilePath` is always a
    folder; the file name may contain sub-folders.
    """
    stem = _output_stem(settings, _scene_or_current(scene), version, wild)
    return os.path.join(_output_directory(settings), stem)


def resolve_output_path(settings, scene=None, create_dirs=True):
    """
    resolve_render_filepath with auto-versioning applied.
    Returns (path_without_extension, version_number_used).
    """
    scene = _scene_or_current(scene)
    path = resolve_render_filepath(settings, scene)
    version = 0
    if settings.saveOutputToFile and settings.autoVersion:
        # Sequences end in '_' and a template may itself end in '_v005'.
        wild_path = resolve_render_filepath(settings, scene, wild=True)
        stem = base_stem_of(os.path.basename(wild_path).rstrip('_'))
        version = scan_existing_versions(os.path.dirname(path), stem) + 1
        path = resolve_render_filepath(settings, scene, version)
    directory = os.path.dirname(path)
    if create_dirs and directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
    return path, version


def preview_filename(settings, scene):
    """
    Name of the file the next playblast will write, for the settings panel.
    I/O-free (safe in a draw): '_v###' stands for the next free version and
    '####' for the frame number of an image sequence.
    """
    stem = os.path.basename(resolve_render_filepath(settings, scene))
    if settings.autoVersion:
        stem = "%s_v%s" % (base_stem_of(stem.rstrip('_')),
                           "#" * int(settings.versionPadding))
    if output_is_video(settings):
        return stem + CONTAINER_EXTENSIONS.get(settings.outputEncoding, '.mp4')
    return stem.rstrip('_') + "_####" + IMAGE_FORMAT_EXTENSIONS.get(
        settings.imageEncoding, '.png')


# =========================================================================
#  Context & viewport overrides
# =========================================================================

def iter_view3d_areas(context):
    """
    Yields (window, area) for every 3D Viewport, current window first so the
    viewport the user is looking at always wins (this mirrors Blender's own
    BKE_screen_find_big_area behaviour, which searches the current screen).
    """
    seen = set()
    windows = []
    try:
        if context.window is not None:
            windows.append(context.window)
    except Exception:
        pass
    try:
        for win in context.window_manager.windows:
            if win not in windows:
                windows.append(win)
    except Exception:
        pass

    for win in windows:
        try:
            areas = list(win.screen.areas)
        except Exception:
            continue
        for area in areas:
            if area.type != 'VIEW_3D':
                continue
            try:
                key = area.as_pointer()
            except Exception:
                key = id(area)
            if key in seen:
                continue
            seen.add(key)
            yield win, area


def area_size(area):
    try:
        return int(area.width) * int(area.height)
    except Exception:
        return 0


def get_view3d_space(area):
    """The VIEW_3D space of an area (never some other space type)."""
    try:
        active = area.spaces.active
        if active is not None and active.type == 'VIEW_3D':
            return active
    except Exception:
        pass
    try:
        for space in area.spaces:
            if space.type == 'VIEW_3D':
                return space
    except Exception:
        pass
    return None


def get_window_region(area):
    """The WINDOW region of an area (the actual viewport canvas)."""
    try:
        for region in area.regions:
            if region.type == 'WINDOW':
                return region
    except Exception:
        pass
    return None


def find_view3d_context(context):
    """
    Locates a usable 3D Viewport.  Returns (window, area, space, region), or
    four Nones when there is none.  The area the operator runs from wins, then
    the current window, then the largest area.
    """
    def parts(win, area):
        space = get_view3d_space(area)
        region = get_window_region(area)
        if space is None or region is None:
            return None
        return win, area, space, region

    try:
        if context.area is not None and context.area.type == 'VIEW_3D':
            found = parts(context.window, context.area)
            if found:
                return found
    except Exception:
        pass

    best = None
    for win, area in iter_view3d_areas(context):
        found = parts(win, area)
        if found is None:
            continue
        score = (win == context.window, area_size(area))
        if best is None or score > best[0]:
            best = (score, found)
    return best[1] if best else (None, None, None, None)


def _override_candidates(context, win, area, region, space):
    """
    Ordered `temp_override` keyword sets, most specific first.

    `screen` is deliberately never passed: it is not a 4.0 keyword at all, and
    4.2+ matches it to the window on its own (blender#116297) while skipping
    the strict screen validation - passing it raises TypeError when e.g. the
    viewport is maximized.
    """
    candidates = []

    full = {}
    if win is not None:
        full['window'] = win
    if area is not None:
        full['area'] = area
    if region is not None:
        full['region'] = region
    if space is not None:
        full['space_data'] = space
    if full:
        candidates.append(full)

    without_space = {k: v for k, v in full.items() if k != 'space_data'}
    if without_space and without_space != full:
        candidates.append(without_space)

    try:
        if (region is not None and area is not None
                and context.area is not None and context.area == area):
            candidates.append({'region': region})
    except Exception:
        pass

    candidates.append({})
    return candidates


def _temp_override_accepted(context, kwargs):
    """
    Probes `temp_override(**kwargs)` without running anything inside it, so
    validation TypeErrors stay separate from operator errors.
    """
    try:
        with context.temp_override(**kwargs):
            pass
    except Exception:
        return False
    return True


def execute_in_view3d(context, operator_func, **kwargs):
    """
    Runs `operator_func` inside the best available 3D Viewport context.
    Returns None when there is no 3D Viewport.
    """
    win, area, space, region = find_view3d_context(context)
    if area is None or space is None:
        return None

    for candidate in _override_candidates(context, win, area, region, space):
        if _temp_override_accepted(context, candidate):
            with context.temp_override(**candidate):
                return operator_func(**kwargs)
    # Nothing accepted: RENDER_OT_opengl can still find a viewport itself.
    return operator_func(**kwargs)


def normalize_operator_result(result):
    """Reduces a bpy.ops return value to a plain set of identifiers."""
    return set(result) if isinstance(result, (set, frozenset)) else set()


def open_confirm_dialog(window_manager, operator, width=460, title="", confirm_text=""):
    """
    `invoke_props_dialog` with graceful degradation: `title`/`confirm_text`
    only exist on newer Blender versions, so fall back step by step.
    """
    attempts = (
        {'width': width, 'title': title, 'confirm_text': confirm_text},
        {'width': width, 'confirm_text': confirm_text},
        {'width': width},
    )
    for kwargs in attempts:
        try:
            return window_manager.invoke_props_dialog(operator, **kwargs)
        except TypeError:
            continue
    return window_manager.invoke_props_dialog(operator)


# =========================================================================
#  Viewport shading
# =========================================================================

def set_shading_on_space(space, color_mode, solid_mode):
    """Applies shading to a single VIEW_3D space, validated against the RNA."""
    if space is None or not hasattr(space, "shading"):
        return False
    applied = assign_enum(space.shading, "type", [color_mode])
    if applied is None:
        return False
    if applied == 'SOLID' and hasattr(space.shading, "color_type"):
        assign_enum(space.shading, "color_type", [solid_mode, 'MATERIAL'])
    return True


# =========================================================================
#  Viewport display control (overlays, gizmos, annotations)
# =========================================================================

# (settings attribute, target, RNA property names, UI label, default)
# `target` is 'overlay' (space.overlay.*) or 'space' (space.*).
# Properties missing from a given release are skipped via hasattr, which is how
# the 5.2-only camera guides/passepartout and the 4.x-only retopology/normals
# toggles are handled without version checks.
DISPLAY_TOGGLES = (
    ('pb_show_annotations',  'space',   ('show_annotation',),              "Annotations",         True),
    ('pb_show_gizmos',       'space',   ('show_gizmo',),                   "Gizmos",              False),
    ('pb_show_floor',        'overlay', ('show_floor',),                   "Floor",               False),
    ('pb_show_axes',         'overlay', ('show_axis_x', 'show_axis_y', 'show_axis_z'), "Axes",    False),
    ('pb_show_cursor',       'overlay', ('show_cursor',),                  "3D Cursor",           False),
    ('pb_show_extras',       'overlay', ('show_extras',),                  "Extras (lights/empties)", False),
    ('pb_show_stats',        'overlay', ('show_stats',),                   "Statistics",          False),
    ('pb_show_origins',      'overlay', ('show_object_origins',),          "Object Origins",      False),
    ('pb_show_rel_lines',    'overlay', ('show_relationship_lines',),      "Relationship Lines",  True),
    ('pb_show_outlines',     'overlay', ('show_outline_selected',),        "Selection Outline",   True),
    ('pb_show_text',         'overlay', ('show_text',),                    "Text",                True),
    ('pb_show_bones',        'overlay', ('show_bones',),                   "Bones",               True),
    ('pb_show_motion_paths', 'overlay', ('show_motion_paths',),            "Motion Paths",        False),
    ('pb_show_onion',        'overlay', ('show_onion_skins',),             "Onion Skins",         False),
    ('pb_show_cam_guides',   'overlay', ('show_camera_guides',),           "Camera Guides (5.2+)", True),
    ('pb_show_cam_passe',    'overlay', ('show_camera_passepartout',),     "Passepartout (5.2+)", True),
)


def display_owner(space, target):
    if target == 'space':
        return space
    return getattr(space, "overlay", None)


def snapshot_display(space):
    """Captures every display property the playblast may touch."""
    snap = {}
    if space is None:
        return snap
    for attr, target, props, _label, _default in DISPLAY_TOGGLES:
        owner = display_owner(space, target)
        if owner is None:
            continue
        for prop in props:
            if hasattr(owner, prop):
                value = get_attr_safe(owner, prop)
                if value is not None:
                    snap[(target, prop)] = bool(value)
    overlay = getattr(space, "overlay", None)
    if overlay is not None and hasattr(overlay, "show_overlays"):
        snap[('overlay', 'show_overlays')] = bool(overlay.show_overlays)
    return snap


def apply_display(space, settings):
    """
    Prepares the viewport for rendering.  Does nothing when
    `autoDisableOverlays` is off.
    """
    if space is None or not settings.autoDisableOverlays:
        return

    overlay = getattr(space, "overlay", None)
    mode = settings.overlayMode

    if mode == 'HIDE_OVERLAYS':
        # Original behaviour: overlay master switch only.
        if overlay is not None:
            set_attr_safe(overlay, "show_overlays", False)
        return

    if mode == 'CLEAN':
        if overlay is not None:
            set_attr_safe(overlay, "show_overlays", False)
        set_attr_safe(space, "show_annotation", False)
        set_attr_safe(space, "show_gizmo", False)
        return

    # CUSTOM: master switch on, then exactly what the user asked for.
    if overlay is not None:
        set_attr_safe(overlay, "show_overlays", True)
    for attr, target, props, _label, default in DISPLAY_TOGGLES:
        owner = display_owner(space, target)
        if owner is None:
            continue
        want = bool(get_attr_safe(settings, attr, default))
        for prop in props:
            if hasattr(owner, prop):
                set_attr_safe(owner, prop, want)


def restore_display(space, snap):
    """Restores display properties. Never raises."""
    if space is None or not snap:
        return
    for (target, prop), value in snap.items():
        owner = display_owner(space, target)
        if owner is not None and hasattr(owner, prop):
            set_attr_safe(owner, prop, value)


# =========================================================================
#  FFmpeg codec & container sanitization
# =========================================================================

def get_video_codec_candidates(container):
    """
    Ordered video-codec preferences per container, each validated against the
    running build (H265/PRORES are 4.5+; 'WEBM' is Blender's identifier for
    VP9 - there is no 'VP9' enum item in any supported version).
    """
    table = {
        'QUICKTIME': ('H264', 'PRORES', 'QTRLE', 'MPEG4'),
        'MPEG4':     ('H264', 'MPEG4'),
        'AVI':       ('MPEG4', 'H264', 'HUFFYUV'),
        'OGG':       ('THEORA',),
        'MKV':       ('H264', 'H265', 'MPEG4'),
        'WEBM':      ('WEBM', 'AV1'),
    }
    return table.get(container, ('H264', 'MPEG4'))


# Container -> audio codecs the muxer accepts, plus the fallback to use.
AUDIO_COMPAT = {
    'OGG':       (('VORBIS', 'FLAC'), 'VORBIS'),
    'WEBM':      (('VORBIS', 'OPUS'), 'VORBIS'),
    'MPEG4':     (('AAC', 'MP3'), 'AAC'),
    'QUICKTIME': (('AAC', 'MP3', 'PCM'), 'AAC'),
    'AVI':       (('MP3', 'PCM', 'AC3'), 'MP3'),
    'MKV':       (('AAC', 'MP3', 'AC3', 'FLAC', 'VORBIS', 'OPUS'), 'AAC'),
}


def get_safe_video_codec(render, container):
    """The video codec identifier that was actually applied."""
    ffmpeg = getattr(render, "ffmpeg", None)
    if ffmpeg is None:
        return None
    return assign_enum(ffmpeg, "codec", get_video_codec_candidates(container))


def get_safe_audio_codec(ffmpeg, container, requested_audio):
    """An audio codec compatible with both the container and the build."""
    if ffmpeg is None:
        return None
    if not requested_audio or requested_audio == 'NONE':
        return assign_enum(ffmpeg, "audio_codec", ['NONE'])

    allowed, fallback = AUDIO_COMPAT.get(container, ((), requested_audio))
    candidates = [requested_audio] if requested_audio in allowed else []
    candidates.extend([fallback] + list(allowed) + ['NONE'])
    return assign_enum(ffmpeg, "audio_codec", candidates)


def apply_ffmpeg_settings_safe(render, settings):
    """Applies FFmpeg settings defensively. Returns the active video codec."""
    ffmpeg = getattr(render, "ffmpeg", None)
    if ffmpeg is None:
        return None

    container = settings.outputEncoding
    assign_enum(ffmpeg, "format", [container, 'MPEG4'])
    codec = get_safe_video_codec(render, container)
    assign_enum(ffmpeg, "constant_rate_factor", [settings.outputQuality, 'MEDIUM'])

    if codec in ('H264', 'H265'):
        assign_enum(ffmpeg, "ffmpeg_preset", ['REALTIME', 'GOOD'])

    audio_codec = get_safe_audio_codec(ffmpeg, container, settings.audioCodec)
    if audio_codec and audio_codec != 'NONE':
        set_attr_safe(ffmpeg, "audio_bitrate",
                      int(max(32, min(2048, settings.audioBitrate))))
        set_attr_safe(ffmpeg, "audio_volume", float(settings.audioVolume))
        if settings.useAudioAdvanced:
            assign_enum(ffmpeg, "audio_channels",
                        [settings.audioChannels, 'STEREO'])
            set_attr_safe(ffmpeg, "audio_mixrate",
                          int(max(8000, min(192000, settings.audioMixrate))))
    return codec


# =========================================================================
#  Metadata stamps
# =========================================================================

STAMP_PROPS = (
    'use_stamp', 'use_stamp_date', 'use_stamp_time',
    'use_stamp_render_time', 'use_stamp_frame',
    'use_stamp_frame_range', 'use_stamp_memory',
    'use_stamp_hostname', 'use_stamp_camera',
    'use_stamp_lens', 'use_stamp_scene',
    'use_stamp_marker', 'use_stamp_filename',
    'use_stamp_sequencer_strip', 'use_stamp_labels',
    'use_stamp_note', 'stamp_note_text',
    'stamp_font_size', 'stamp_foreground', 'stamp_background',
)


def get_safe_stamp_settings(render):
    """Captures the stamp properties present in the current Blender version."""
    snapshot = {}
    for attr in STAMP_PROPS:
        if hasattr(render, attr):
            value = get_attr_safe(render, attr)
            if value is None:
                continue
            snapshot[attr] = list(value) if hasattr(value, '__len__') \
                and not isinstance(value, str) else value
    return snapshot


def build_stamp_note(settings, width, height, version=0):
    """Assembles the multi-line stamp note burned into the render."""
    lines = ["Playblast | Blender {}".format(bpy.app.version_string)]
    if version and getattr(settings, 'metadata_version', True):
        lines.append("Version: v%0*d" % (int(settings.versionPadding), int(version)))
    if settings.metadata_resolution:
        lines.append("Resolution: {} x {} px".format(width, height))
    note = (settings.extraInfo or "").strip()
    if note:
        lines.append(note)
    return "\n".join(lines)


def apply_stamp_settings(render, settings, note_text, final_height):
    """
    Configures the burn-in stamps for the playblast.

    `final_height` is the output height in pixels the pipeline computed
    (PlayblastJob.note_size); the auto font size scales with it.
    """
    for attr in ('use_stamp', 'use_stamp_date', 'use_stamp_time',
                 'use_stamp_frame', 'use_stamp_note'):
        if hasattr(render, attr):
            set_attr_safe(render, attr, True)

    for attr in ('use_stamp_render_time', 'use_stamp_frame_range',
                 'use_stamp_memory', 'use_stamp_hostname',
                 'use_stamp_marker', 'use_stamp_filename',
                 'use_stamp_sequencer_strip', 'use_stamp_labels'):
        if hasattr(render, attr):
            set_attr_safe(render, attr, False)

    if hasattr(render, 'use_stamp_camera'):
        set_attr_safe(render, 'use_stamp_camera', bool(settings.metadata_camera_name))
    if hasattr(render, 'use_stamp_lens'):
        set_attr_safe(render, 'use_stamp_lens', bool(settings.metadata_camera_lens))
    if hasattr(render, 'use_stamp_scene'):
        set_attr_safe(render, 'use_stamp_scene', bool(settings.metadata_scene_name))
    if hasattr(render, 'stamp_note_text'):
        set_attr_safe(render, 'stamp_note_text', note_text)

    # Font size always applies; Blender's default (12 px) is unreadable at HD+.
    if hasattr(render, 'stamp_font_size'):
        try:
            size = (final_height / 45.0 if settings.stampAutoSize
                    else settings.stampFontSize)   # ~24 px at 1080p
            render.stamp_font_size = int(max(8, min(200, size)))
        except Exception:
            pass

    if settings.useStampStyle:
        if hasattr(render, 'use_stamp_labels'):
            set_attr_safe(render, 'use_stamp_labels', bool(settings.stampUseLabels))
        if hasattr(render, 'stamp_foreground'):
            set_attr_safe(render, 'stamp_foreground', list(settings.stampFgColor))
        if hasattr(render, 'stamp_background'):
            set_attr_safe(render, 'stamp_background', list(settings.stampBgColor))


# =========================================================================
#  Output format helpers (media_type aware - Blender 5.0+)
# =========================================================================

def get_media_type(image_settings):
    """The current media_type, or None on Blender 4.x."""
    if not hasattr(image_settings, "media_type"):
        return None
    return get_attr_safe(image_settings, "media_type")


def apply_output_format(render, settings, want_video,
                        container=None, image_format=None):
    """
    Configures `render.image_settings` for a movie or an image sequence.

    `container` / `image_format` override the values read from `settings`;
    playback passes the format RECORDED for an existing playblast, and with a
    container override only the container is touched (the codec settings of
    an existing movie are irrelevant to playback).

    ORDER MATTERS on 5.0+: the `file_format` enum is filtered by `media_type`,
    so 'FFMPEG' is only assignable while media_type is 'VIDEO'.  On 4.x
    `media_type` does not exist and `assign_enum` quietly skips it.
    """
    image_settings = render.image_settings

    if want_video:
        assign_enum(image_settings, "media_type", ['VIDEO'])
        if assign_enum(image_settings, "file_format", ['FFMPEG']) is None:
            return False, ("FFmpeg video output is not available in this Blender "
                           "build. Choose 'Image Sequence' instead.")
        if container is not None:
            ffmpeg = getattr(render, "ffmpeg", None)
            if ffmpeg is not None:
                assign_enum(ffmpeg, "format", [container, 'MPEG4'])
        else:
            apply_ffmpeg_settings_safe(render, settings)
        return True, ""

    assign_enum(image_settings, "media_type", ['IMAGE'])
    requested = image_format or settings.imageEncoding or 'PNG'
    if assign_enum(image_settings, "file_format", [requested, 'PNG']) is None:
        return False, ("The selected image format is not available in this "
                       "Blender build.")
    return True, ""


def restore_output_format(render, file_format, media_type):
    """
    Restores the output format: media_type FIRST, then file_format.  The
    reverse order raises on 5.x because 'FFMPEG' is not a legal file_format
    while media_type is 'IMAGE'.
    """
    image_settings = render.image_settings

    if media_type is not None and hasattr(image_settings, "media_type"):
        set_attr_safe(image_settings, "media_type", media_type)

    if file_format:
        if not set_attr_safe(image_settings, "file_format", file_format):
            set_attr_safe(image_settings, "media_type",
                          'VIDEO' if file_format == 'FFMPEG' else 'IMAGE')
            set_attr_safe(image_settings, "file_format", file_format)


# =========================================================================
#  State snapshot: everything a playblast modifies, captured & restored
# =========================================================================

class StateSnapshot:
    """
    Captures every scene/viewport value the playblast pipeline may modify and
    puts it all back afterwards.

    `restore()` runs inside a `finally` block and must never raise: an
    exception there would leave the user's scene settings modified.
    """

    FFMPEG_ATTRS = ('format', 'codec', 'constant_rate_factor', 'ffmpeg_preset',
                    'audio_codec', 'audio_bitrate', 'audio_volume',
                    'audio_channels', 'audio_mixrate')

    def __init__(self, context, render, space):
        image_settings = render.image_settings

        ffmpeg_snap = {}
        ffmpeg = getattr(render, "ffmpeg", None)
        if ffmpeg is not None:
            for attr in self.FFMPEG_ATTRS:
                if hasattr(ffmpeg, attr):
                    value = get_attr_safe(ffmpeg, attr)
                    if value is not None:
                        ffmpeg_snap[attr] = value

        rv3d = get_attr_safe(space, "region_3d", None)
        shading = getattr(space, "shading", None)

        self.render = render
        self.space = space
        self.rv3d = rv3d
        self.data = {
            'resolution_x': render.resolution_x,
            'resolution_y': render.resolution_y,
            'resolution_percentage': render.resolution_percentage,
            'filepath': render.filepath,
            'use_overwrite': render.use_overwrite,
            'use_file_extension': render.use_file_extension,
            'file_format': image_settings.file_format,
            'media_type': get_media_type(image_settings),
            'ffmpeg': ffmpeg_snap,
            'stamps': get_safe_stamp_settings(render),
            'frame_start': context.scene.frame_start,
            'frame_end': context.scene.frame_end,
            'frame_step': get_attr_safe(context.scene, 'frame_step', 1),
            'frame_current': context.scene.frame_current,
            'use_preview_range': context.scene.use_preview_range,
            'save_output': get_attr_safe(render, 'save_output'),
            'display': snapshot_display(space),
            'shading_type': get_attr_safe(shading, "type"),
            'shading_color': get_attr_safe(shading, "color_type"),
            'rv3d_persp': get_attr_safe(rv3d, "view_perspective"),
        }

    def restore(self, context, settings):
        """Puts every captured value back. Never raises."""
        render = self.render
        data = self.data
        scene = getattr(context, "scene", None)

        ffmpeg = getattr(render, "ffmpeg", None)
        if ffmpeg is not None:
            for attr, value in data.get('ffmpeg', {}).items():
                set_attr_safe(ffmpeg, attr, value)

        for attr, value in data.get('stamps', {}).items():
            set_attr_safe(render, attr, value)

        # media_type BEFORE file_format (5.x filters file_format by media_type).
        restore_output_format(render, data.get('file_format'), data.get('media_type'))

        set_attr_safe(render, 'resolution_x', data.get('resolution_x'))
        set_attr_safe(render, 'resolution_y', data.get('resolution_y'))
        set_attr_safe(render, 'resolution_percentage', data.get('resolution_percentage'))
        set_attr_safe(render, 'filepath', data.get('filepath'))
        set_attr_safe(render, 'use_overwrite', data.get('use_overwrite'))
        set_attr_safe(render, 'use_file_extension', data.get('use_file_extension'))

        if data.get('save_output') is not None:
            set_attr_safe(render, 'save_output', data['save_output'])

        if scene is not None:
            set_attr_safe(scene, 'use_preview_range', data.get('use_preview_range'))
            set_attr_safe(scene, 'frame_start', data.get('frame_start'))
            set_attr_safe(scene, 'frame_end', data.get('frame_end'))
            set_attr_safe(scene, 'frame_step', data.get('frame_step'))
            try:
                if data.get('frame_current') is not None:
                    scene.frame_set(int(data['frame_current']))
            except Exception:
                pass

        space = self.space
        if space is None:
            try:
                _, _, space, _ = find_view3d_context(context)
            except Exception:
                space = None

        if space is not None:
            restore_display(space, data.get('display'))

            shading = getattr(space, "shading", None)
            if (shading is not None and settings.restore_shading
                    and settings.ColorMode != 'CURRENT'):
                if data.get('shading_type'):
                    assign_enum(shading, "type", [data['shading_type']])
                if data.get('shading_color') and hasattr(shading, "color_type"):
                    assign_enum(shading, "color_type", [data['shading_color']])

        if self.rv3d is not None and data.get('rv3d_persp'):
            assign_enum(self.rv3d, "view_perspective", [data['rv3d_persp']])

        try:
            for win in context.window_manager.windows:
                for win_area in win.screen.areas:
                    if win_area.type == 'VIEW_3D':
                        win_area.tag_redraw()
        except Exception:
            pass


# =========================================================================
#  Presets
# =========================================================================

MAX_HISTORY = 12

# One-click starting points shown above the preset menu.  Applied through
# dict_to_settings, so every value is validated like a user preset.
BUILTIN_PRESETS = {
    "Draft": {
        'saveOutputToFile': True, 'resolutionMode': 'LIST',
        'resolutionTemplate': 'x1280y720', 'resolution_percentage': 100,
        'useFrameStep': True, 'frameStep': 2, 'keyframesOnly': False,
        'outputEncoding': 'MPEG4', 'outputQuality': 'LOW', 'audioCodec': 'NONE',
    },
    "Review": {
        'saveOutputToFile': True, 'resolutionMode': 'LIST',
        'resolutionTemplate': 'x1920y1080', 'resolution_percentage': 100,
        'useFrameStep': False, 'keyframesOnly': False,
        'outputEncoding': 'MPEG4', 'outputQuality': 'MEDIUM', 'audioCodec': 'AAC',
    },
    "Final": {
        'saveOutputToFile': True, 'resolutionMode': 'LIST',
        'resolutionTemplate': 'x1920y1080', 'resolution_percentage': 100,
        'useFrameStep': False, 'keyframesOnly': False,
        'outputEncoding': 'MPEG4', 'outputQuality': 'HIGH', 'audioCodec': 'AAC',
    },
}

# Runtime state: never written into a preset, never mirrored to the settings
# operator.
BOOKKEEPING_PROPS = frozenset((
    'history', 'history_index', 'preset_active',
    'last_playblast_path', 'last_playblast_format',
    'last_playblast_use_ffmpeg', 'last_playblast_encoding',
    'last_playblast_file', 'last_render_seconds', 'last_render_frames',
))

# Machine-specific values: still mirrored to the settings operator (the panel
# edits them) but never written into a preset, so presets transfer between
# machines and projects.  NOTE: do not put these in BOOKKEEPING_PROPS - that
# would also drop them from MIRRORED_NAMES and break the settings panel.
NON_PRESET_PROPS = frozenset(('outputFilePath',))


def is_preset_prop(name):
    """True for user-facing settings (not runtime bookkeeping)."""
    return (name != 'rna_type' and name not in BOOKKEEPING_PROPS
            and name not in NON_PRESET_PROPS)


def preset_directory(create=False):
    """Folder for user preset JSON files, with progressively safer fallbacks."""
    try:
        path = bpy.utils.user_resource('CONFIG', path="playblast_presets", create=create)
        if path:
            if create and not os.path.isdir(path):
                os.makedirs(path, exist_ok=True)
            return path
    except Exception:
        pass
    try:
        base = bpy.utils.user_resource('CONFIG')
        if base:
            path = os.path.join(base, "playblast_presets")
            if create and not os.path.isdir(path):
                os.makedirs(path, exist_ok=True)
            return path
    except Exception:
        pass
    return os.path.join(get_default_temp_dir(), "playblast_presets")


def list_presets():
    """Sorted preset names (file stems) available to the user."""
    directory = preset_directory(create=False)
    if not directory or not os.path.isdir(directory):
        return []
    try:
        names = [os.path.splitext(f)[0] for f in os.listdir(directory)
                 if f.lower().endswith('.json')]
    except Exception:
        return []
    return sorted(n for n in names if n)


def settings_to_dict(settings):
    """Serialises every user-facing setting via RNA introspection."""
    data = {}
    try:
        props = list(settings.bl_rna.properties)
    except Exception:
        return data
    for prop in props:
        name = getattr(prop, 'identifier', None)
        if not name or not is_preset_prop(name):
            continue
        if getattr(prop, 'is_readonly', False):
            continue
        value = get_attr_safe(settings, name)
        if value is None:
            continue
        try:
            if isinstance(value, (bool, int, float, str)):
                data[name] = value
            elif hasattr(value, '__len__'):
                data[name] = [float(v) for v in value]
        except Exception:
            continue
    return data


def dict_to_settings(settings, data):
    """
    Restores settings from a preset dict, validating every value against the
    live RNA so a preset from another Blender version can never corrupt state.
    Returns the number of properties applied.
    """
    if not isinstance(data, dict):
        return 0
    applied = 0
    try:
        props = settings.bl_rna.properties
    except Exception:
        return 0

    for name, value in data.items():
        if not is_preset_prop(name):
            continue
        try:
            prop = props.get(name)
        except Exception:
            prop = None
        if prop is None:
            continue
        ptype = getattr(prop, 'type', None)
        try:
            if ptype == 'ENUM':
                identifiers = {i.identifier for i in prop.items}
                if value in identifiers:
                    setattr(settings, name, value)
                    applied += 1
            elif ptype == 'BOOLEAN':
                setattr(settings, name, bool(value))
                applied += 1
            elif ptype == 'INT':
                setattr(settings, name, int(value))
                applied += 1
            elif ptype == 'FLOAT':
                if getattr(prop, 'is_array', False):
                    setattr(settings, name, [float(v) for v in value])
                else:
                    setattr(settings, name, float(value))
                applied += 1
            elif ptype == 'STRING':
                setattr(settings, name, str(value))
                applied += 1
        except Exception:
            continue
    return applied


def preset_file_stem(name):
    """Preset names are file stems: 'a/../b' must never leave the folder."""
    stem = sanitize_filename(re.sub(r'[\\/]+', "_", str(name or "")))
    return stem.lstrip(".")


def save_preset(name, settings):
    """Writes a preset file. Returns (ok, message)."""
    name = preset_file_stem(name) or "preset"
    if name in BUILTIN_PRESETS:
        name += "_custom"      # built-ins always win on load: never shadow one
    directory = preset_directory(create=True)
    try:
        if not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name + ".json")
        payload = {
            'addon': 'Playblast Animation Plugin',
            'schema': 1,
            'blender': list(bpy.app.version),
            'saved': time.strftime("%Y-%m-%d %H:%M:%S"),
            'settings': settings_to_dict(settings),
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        return True, path
    except Exception as exc:
        return False, str(exc)


def load_preset(name, settings):
    """Applies a built-in or saved preset. Returns (ok, message)."""
    if name in BUILTIN_PRESETS:
        count = dict_to_settings(settings, BUILTIN_PRESETS[name])
        return True, "Applied the '%s' preset (%d settings)." % (name, count)
    directory = preset_directory(create=False)
    path = os.path.join(directory, preset_file_stem(name) + ".json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        return False, "Could not read preset '%s': %s" % (name, exc)

    data = payload.get('settings') if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return False, "Preset '%s' has no settings block." % name
    count = dict_to_settings(settings, data)
    return True, "Loaded %d setting(s) from '%s'." % (count, name)


def delete_preset(name):
    """Removes a preset file. Returns (ok, message)."""
    directory = preset_directory(create=False)
    path = os.path.join(directory, preset_file_stem(name) + ".json")
    try:
        os.remove(path)
        return True, "Deleted preset '%s'." % name
    except Exception as exc:
        return False, "Could not delete '%s': %s" % (name, exc)


def set_active_preset(settings, name):
    """
    Remembers which preset is current.

    Deliberately a plain StringProperty, NOT an EnumProperty with dynamic
    items: Blender validates an enum's `default` against its items at
    registration time, so an items callback whose contents change (preset
    files appearing/disappearing) makes the whole PropertyGroup fail to
    register with "'preset_active' EnumProperty could not register".  A menu
    is what Blender itself uses for presets (see RENDER_MT_presets).
    """
    try:
        known = name in BUILTIN_PRESETS or name in list_presets()
        settings.preset_active = name if known else ""
    except Exception:
        pass
    return settings.preset_active


# =========================================================================
#  Summary / ETA helpers
# =========================================================================

def effective_frame_range(settings, scene):
    """The frame range and step the playblast will actually use."""
    if settings.frameRangeMode == 'CUSTOM':
        start = min(settings.frameRangeStart, settings.frameRangeEnd)
        end = max(settings.frameRangeStart, settings.frameRangeEnd)
    elif settings.frameRangeMode == 'PREVIEW':
        start, end = int(scene.frame_preview_start), int(scene.frame_preview_end)
    else:
        if scene.use_preview_range:
            start, end = int(scene.frame_preview_start), int(scene.frame_preview_end)
        else:
            start, end = int(scene.frame_start), int(scene.frame_end)
    if settings.useFrameStep:
        step = max(1, int(settings.frameStep))
    else:
        step = max(1, int(get_attr_safe(scene, 'frame_step', 1) or 1))
    return start, end, step


def frame_count(start, end, step):
    if end < start:
        return 0
    return (end - start) // max(1, step) + 1


def scene_fps(scene):
    """Effective frames per second of the scene."""
    try:
        return scene.render.fps / (scene.render.fps_base or 1.0)
    except Exception:
        return 24.0


def estimate_output(settings, scene):
    """
    Returns a dict describing what is about to be rendered, for the confirm
    dialog and the settings panel read-out.  Accepts either the scene settings
    group or the settings operator as `settings` (same attribute names).
    """
    start, end, step = effective_frame_range(settings, scene)
    frames = frame_count(start, end, step)

    if settings.resolutionMode == 'LIST':
        res_x, res_y = parse_resolution(settings.resolutionTemplate)
    elif settings.resolutionMode == 'CUSTOM':
        res_x, res_y = int(settings.resolutionX), int(settings.resolutionY)
    else:
        res_x, res_y = int(scene.render.resolution_x), int(scene.render.resolution_y)

    pct = (int(scene.render.resolution_percentage) if settings.resolutionMode == 'SYNC'
           else int(settings.resolution_percentage))
    width = max(1, int(round(res_x * pct / 100.0)))
    height = max(1, int(round(res_y * pct / 100.0)))

    fps = scene_fps(scene)
    duration = (frames / fps) if fps > 0 else 0.0

    eta = None
    last_frames = float(get_attr_safe(settings, 'last_render_frames', 0) or 0)
    last_secs = float(get_attr_safe(settings, 'last_render_seconds', 0.0) or 0.0)
    if last_frames > 0 and last_secs > 0 and frames > 0:
        eta = (last_secs / last_frames) * frames

    return {
        'start': start, 'end': end, 'step': step, 'frames': frames,
        'width': width, 'height': height,
        'fps': fps, 'duration': duration, 'eta': eta,
        'video': output_is_video(settings),
        'container': settings.outputEncoding,
    }


def format_duration(seconds):
    if seconds is None:
        return "unknown"
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return "%.1fs" % seconds
    minutes, secs = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return "%dm %02ds" % (minutes, secs)
    hours, minutes = divmod(minutes, 60)
    return "%dh %02dm" % (hours, minutes)


# =========================================================================
#  Property groups
# =========================================================================

class PlayblastHistoryItem(PropertyGroup):
    """One entry of the recent-playblasts list."""
    path: StringProperty(default="")
    file: StringProperty(default="")
    is_video: BoolProperty(default=False)
    fmt: StringProperty(default="")
    encoding: StringProperty(default="")
    frames: IntProperty(default=0)
    seconds: FloatProperty(default=0.0)
    stamp: StringProperty(default="")
    label: StringProperty(default="")


class PlayblastSettingsGroup(PropertyGroup):
    # ---- Resolution ----
    resolutionModeContainer = (
        ('SYNC',   "Use Scene Resolution", "Use the current scene render resolution"),
        ('LIST',   "Resolution List", "Choose from a preset resolution"),
        ('CUSTOM', "Custom Resolution", "Specify custom pixel dimensions"),
    )
    resolutionMode: EnumProperty(
        name="Resolution Mode", items=resolutionModeContainer, default='LIST')

    resolutionTemplateContainer = (
        ('x3840y2160', "3840 x 2160 (16:9) UHD 4K", ""),
        ('x2560y1440', "2560 x 1440 (16:9) QHD 1440p", ""),
        ('x1920y1080', "1920 x 1080 (16:9) HD 1080p", ""),
        ('x1280y720',  "1280 x 720 (16:9) HD 720p", ""),
        ('x854y480',   "854 x 480 (16:9) 480p", ""),
        ('x640y360',   "640 x 360 (16:9) 360p", ""),
        ('x1920y1440', "1920 x 1440 (4:3)", ""),
        ('x1600y1200', "1600 x 1200 (4:3)", ""),
        ('x1280y960',  "1280 x 960 (4:3)", ""),
        ('x1024y768',  "1024 x 768 (4:3)", ""),
        ('x960y720',   "960 x 720 (4:3)", ""),
        ('x800y600',   "800 x 600 (4:3)", ""),
        ('x640y480',   "640 x 480 (4:3)", ""),
        ('x1024y1024', "1024 x 1024 (1:1)", ""),
        ('x512y512',   "512 x 512 (1:1)", ""),
        ('x200y200',   "200 x 200 (1:1)", ""),
    )
    resolutionTemplate: EnumProperty(
        name="Resolution List", items=resolutionTemplateContainer, default='x1920y1080')

    resolutionX: IntProperty(name="Resolution X", subtype='PIXEL', default=1920,
                             min=4, max=16384, soft_max=4096)
    resolutionY: IntProperty(name="Resolution Y", subtype='PIXEL', default=1080,
                             min=4, max=16384, soft_max=4096)
    resolution_percentage: IntProperty(name="Resolution %", subtype='PERCENTAGE',
                                       default=100, min=1, max=1000, soft_max=100)

    # ---- Frame range / speed ----
    frameRangeContainer = (
        ('SCENE',   "Scene Range",
         "Render the scene frame range (the preview range is used when enabled)"),
        ('PREVIEW', "Force Preview Range", "Temporarily enable the preview range"),
        ('CUSTOM',  "Custom Range", "Temporarily render a custom frame range"),
    )
    frameRangeMode: EnumProperty(name="Frame Range", items=frameRangeContainer,
                                 default='SCENE')
    frameRangeStart: IntProperty(name="Start", default=1, min=-1048574, max=1048574)
    frameRangeEnd: IntProperty(name="End", default=250, min=-1048574, max=1048574)

    useFrameStep: BoolProperty(
        name="Frame Step",
        description="Render every Nth frame only - a fast draft at the cost of smoothness",
        default=False)
    frameStep: IntProperty(
        name="Step", default=1, min=1, max=250,
        description="Render every Nth frame (scene.frame_step is set temporarily)")

    keyframesOnly: BoolProperty(
        name="Keyframes Only",
        description="Render only frames where the SELECTED objects have a key "
                    "(uses RENDER_OT_opengl's render_keyed_only)",
        default=False)

    # ---- Output ----
    # '//' = next to the .blend.  The old default (Blender's session temp
    # folder) is deleted when Blender exits.  Unsaved files still fall back to
    # the temp folder, with a visible warning (see output_directory_notice).
    outputFilePath: StringProperty(name="File Path", subtype="DIR_PATH",
                                   default="//playblasts/")
    outputFileName: StringProperty(
        name="File Name", default="{blend}_{start}-{end}",
        description="Base name without extension. By default the .blend file "
                    "name plus the frame range; tokens are enabled by default: "
                    + TOKEN_HELP)

    useNameTokens: BoolProperty(
        name="Use Name Tokens",
        description="Expand {tokens} in the file name: " + TOKEN_HELP,
        default=True)
    autoVersion: BoolProperty(
        name="Auto Version",
        description="Append _v001, _v002 ... and bump to the next free number "
                    "instead of overwriting. Unlimited: v100, v1000, ... all work",
        default=True)
    versionPadding: IntProperty(name="Version Digits", default=3, min=1, max=6)

    saveOutputToFile: BoolProperty(
        name="Save to file",
        description="Write the playblast to the output folder. When disabled a temporary "
                    "PNG sequence is used and the format options are ignored",
        default=True)

    encodingContainer = (
        ('MPEG4',     "MPEG-4 (.mp4)", "Studio review standard: small H.264 file"),
        ('QUICKTIME', "Quicktime (.mov)", ""),
        ('AVI',       "AVI (.avi)", ""),
        ('OGG',       "Ogg (.ogv)", ""),
        ('MKV',       "Matroska (.mkv)", ""),
        ('WEBM',      "WebM (.webm)", ""),
        ('IMAGE',     "Image Sequence", ""),
    )
    outputEncoding: EnumProperty(name="Format", items=encodingContainer,
                                 default='MPEG4')

    imageEncodingContainer = (
        ('PNG',      "PNG", ""),
        ('JPEG',     "JPEG", ""),
        ('JPEG2000', "JPEG 2000", ""),
        ('OPEN_EXR', "OpenEXR", ""),
        ('CINEON',   "Cineon", ""),
        ('DPX',      "DPX", ""),
        ('BMP',      "BMP", ""),
        ('TARGA',    "Targa", ""),
        ('TIFF',     "TIFF", ""),
    )
    imageEncoding: EnumProperty(name="Encoding", items=imageEncodingContainer,
                                default='PNG')

    videoQualityContainer = (
        ('LOWEST',   "Lowest", ""),
        ('VERYLOW',  "Very low", ""),
        ('LOW',      "Low", ""),
        ('MEDIUM',   "Medium", ""),
        ('HIGH',     "High", ""),
        ('LOSSLESS', "Lossless", ""),
    )
    outputQuality: EnumProperty(name="Quality", items=videoQualityContainer,
                                default='MEDIUM')

    # ---- Audio ----
    audioCodecContainer = (
        ('NONE',   "No Audio", "Disables audio output"),
        ('AAC',    "AAC", "Standard review audio codec"),
        ('MP3',    "MP3", ""),
        ('AC3',    "AC3", ""),
        ('FLAC',   "FLAC", ""),
        ('OPUS',   "Opus", ""),
        ('VORBIS', "Vorbis", ""),
        ('PCM',    "PCM (Uncompressed)", ""),
    )
    audioCodec: EnumProperty(name="Audio", items=audioCodecContainer, default='AAC')
    audioBitrate: IntProperty(name="Bitrate", min=32, max=2048, soft_max=384, default=192,
                              description="Audio bitrate (kb/s)")
    audioVolume: FloatProperty(name="Volume", min=0.0, max=1.0, default=1.0)

    useAudioAdvanced: BoolProperty(
        name="Advanced Audio",
        description="Also set the channel layout and sample rate",
        default=False)
    audioChannelsContainer = (
        ('MONO',      "Mono", ""),
        ('STEREO',    "Stereo", ""),
        ('SURROUND4', "4 Channels", ""),
        ('SURROUND51', "5.1 Surround", ""),
        ('SURROUND71', "7.1 Surround", ""),
    )
    audioChannels: EnumProperty(name="Channels", items=audioChannelsContainer,
                                default='STEREO')
    audioMixrate: IntProperty(name="Sample Rate", default=48000, min=8000, max=192000,
                              description="Audio sample rate (samples/s)")

    # ---- Viewport shading ----
    SolidmodeContainer = (
        ('MATERIAL', "Material", ""),
        ('OBJECT',   "Object", ""),
        ('VERTEX',   "Vertex", ""),
        ('SINGLE',   "Single", ""),
        ('RANDOM',   "Random", ""),
        ('TEXTURE',  "Texture", ""),
    )
    SolidMode: EnumProperty(name="Color", items=SolidmodeContainer,
                            default='MATERIAL')

    ColorContainer = (
        ('CURRENT',   "Keep Current", "Use current viewport shading as-is"),
        ('SOLID',     "Solid", "Switch to Solid shading"),
        ('MATERIAL',  "Material Preview", "Switch to Material Preview shading"),
        ('WIREFRAME', "Wireframe", "Switch to Wireframe shading"),
        ('RENDERED',  "Rendered", "Switch to Rendered shading - can be very slow"),
    )
    ColorMode: EnumProperty(name="Viewport Shading", items=ColorContainer,
                            default='CURRENT')

    restore_shading: BoolProperty(
        name="Revert Shading After Playblast",
        description="Revert viewport shading to its pre-render state when done "
                    "(shading is only changed for the playblast itself)",
        default=True)

    useCameraView: BoolProperty(
        name="Playblast From Camera",
        description="Render the playblast through the scene camera (on by default). "
                    "Temporarily switches the viewport to camera perspective for the "
                    "playblast, then restores the previous view. If the scene has no "
                    "camera, falls back to the current viewport view",
        default=True)

    # ---- Display / overlays ----
    autoDisableOverlays: BoolProperty(
        name="Adjust Display",
        description="Change viewport display for the playblast and restore it afterwards",
        default=True)

    overlayModeContainer = (
        ('HIDE_OVERLAYS', "Hide Overlays",
         "Turn off the overlay master switch only (annotations and gizmos stay visible)"),
        ('CLEAN', "Clean Plate", "Hide overlays, gizmos and annotations"),
        ('CUSTOM', "Custom", "Choose exactly what stays visible"),
    )
    overlayMode: EnumProperty(name="Display Mode", items=overlayModeContainer,
                              default='HIDE_OVERLAYS')

    # pb_show_* toggles are generated from DISPLAY_TOGGLES (see below).

    # ---- Stamps ----
    metadata_resolution: BoolProperty(name="Resolution", default=True)
    metadata_version: BoolProperty(
        name="Version", default=True,
        description="Burn the auto-version number (v003) into the render, "
                    "so a reviewer always knows which take they are looking at")
    metadata_camera_name: BoolProperty(name="Camera Name", default=False)
    metadata_camera_lens: BoolProperty(name="Lens", default=False)
    metadata_scene_name: BoolProperty(name="Scene Name", default=True)
    extraInfo: StringProperty(name="Note", default="",
                              description="Extra line burned into the render stamp")

    useStampStyle: BoolProperty(
        name="Custom Stamp Style",
        description="Override the stamp font size, colours and labels for this playblast",
        default=False)
    stampAutoSize: BoolProperty(
        name="Auto Font Size", default=True,
        description="Scale the stamp text with the render height so it stays readable")
    stampFontSize: IntProperty(name="Font Size", default=24, min=8, max=200)
    stampUseLabels: BoolProperty(name="Show Labels", default=True,
                                 description="Prefix values with their label ('Camera: ...')")
    stampFgColor: FloatVectorProperty(name="Text Color", subtype='COLOR', size=4,
                                      min=0.0, max=1.0,
                                      default=(0.8, 0.8, 0.8, 1.0))
    stampBgColor: FloatVectorProperty(name="Background Color", subtype='COLOR', size=4,
                                      min=0.0, max=1.0,
                                      default=(0.0, 0.0, 0.0, 0.25))

    # ---- Playback ----
    autoPlay: BoolProperty(
        name="Play After Render",
        description="Open the result in the animation player as soon as it finishes",
        default=True)

    # ---- Presets ----
    preset_active: StringProperty(
        name="Preset", default="",
        description="Name of the preset that was loaded or saved last")

    # ---- Bookkeeping (never saved into presets) ----
    last_playblast_path: StringProperty(default="")
    last_playblast_format: StringProperty(default="")
    last_playblast_use_ffmpeg: BoolProperty(default=False)
    last_playblast_encoding: StringProperty(default="")
    last_playblast_file: StringProperty(default="")
    last_render_seconds: FloatProperty(default=0.0)
    last_render_frames: IntProperty(default=0)

    history: CollectionProperty(type=PlayblastHistoryItem)
    history_index: IntProperty(default=0)


# One BoolProperty per DISPLAY_TOGGLES row: the table is the single source of
# truth for attribute name, label and default.
for _attr, _target, _props, _label, _default in DISPLAY_TOGGLES:
    PlayblastSettingsGroup.__annotations__[_attr] = BoolProperty(
        name=_label, default=_default)


# =========================================================================
#  Settings mirroring: scene property group <-> settings operator
#
#  The Adjust Last Operation panel restores the undo state and re-runs the
#  operator on EVERY edit inside it, so interactive controls must be operator
#  properties (this is exactly how Blender's own sphere-cuts panel works).
#  The operator reuses the very same property definitions as the property
#  group - one source of truth - and execute() syncs operator -> scene.
# =========================================================================

MIRRORED_NAMES = tuple(name for name in PlayblastSettingsGroup.__annotations__
                       if name not in BOOKKEEPING_PROPS)


def _plain_value(value):
    """bpy_prop_array -> tuple; everything else untouched."""
    if hasattr(value, '__len__') and not isinstance(value, str):
        return tuple(value)
    return value


def sync_pg_to_operator(op, settings):
    """Copies every mirrored setting from the scene group onto the operator."""
    for name in MIRRORED_NAMES:
        try:
            setattr(op, name, _plain_value(getattr(settings, name)))
        except Exception:
            pass


def sync_operator_to_pg(op, settings):
    """Copies every mirrored setting from the operator onto the scene group."""
    for name in MIRRORED_NAMES:
        try:
            setattr(settings, name, _plain_value(getattr(op, name)))
        except Exception:
            pass


def reopen_settings():
    """
    Re-runs the settings operator so the Adjust Last Operation panel survives
    operations launched from inside it (preset load/save/delete) - those operators would otherwise become the redo target.
    """
    try:
        bpy.ops.my.playblast_settings('INVOKE_DEFAULT')
    except Exception as exc:
        LOG.debug("Could not reopen the settings panel: %s", exc)


# =========================================================================
#  History helpers
# =========================================================================

def push_history(settings, path, written_file, is_video, fmt, encoding,
                 frames, seconds):
    """Adds an entry at the top of the recent list and prunes old ones."""
    try:
        history = settings.history
    except Exception:
        return False
    try:
        item = history.add()
        item.path = path or ""
        item.file = written_file or ""
        item.is_video = bool(is_video)
        item.fmt = fmt or ""
        item.encoding = encoding or ""
        item.frames = int(frames or 0)
        item.seconds = float(seconds or 0.0)
        item.stamp = time.strftime("%Y-%m-%d %H:%M")
        item.label = os.path.basename(written_file or path or "playblast")
        try:
            history.move(len(history) - 1, 0)
        except Exception:
            pass
        while len(history) > MAX_HISTORY:
            history.remove(len(history) - 1)
        return True
    except Exception:
        LOG.exception("Could not record playblast history")
        return False


def history_entry(settings, index):
    try:
        if 0 <= index < len(settings.history):
            return settings.history[index]
    except Exception:
        pass
    return None


# =========================================================================
#  Layout drawing
#
#  Everything here is drawn inside the Adjust Last Operation panel, which is
#  NARROW by default.  Therefore: no property-split layouts (they squeeze the
#  value column and clip labels), one control per row where text is long, and
#  help text split into short lines.  Interactive controls are drawn from the
#  OPERATOR (`owner`), read-only read-outs from the scene group.
# =========================================================================

def draw_summary(layout, settings, scene, icon='INFO'):
    """Compact read-out of what the next playblast will produce."""
    info = estimate_output(settings, scene)
    box = layout.box()
    col = box.column(align=True)
    col.label(text="%d frames (%d-%d, step %d)"
                   % (info['frames'], info['start'], info['end'], info['step']),
              icon=icon)
    col.label(text="%d x %d px   %s"
              % (info['width'], info['height'], format_duration(info['duration'])))
    if getattr(settings, 'useCameraView', False) and getattr(scene, 'camera', None):
        col.label(text="View: camera '%s'" % scene.camera.name, icon='CAMERA_DATA')
    else:
        col.label(text="View: current viewport", icon='VIEW3D')
    kind = info['container'] if info['video'] else ("%s sequence" % settings.imageEncoding)
    col.label(text="Output: %s" % kind)
    if info['eta'] is not None:
        col.label(text="Est. render: %s (last run)" % format_duration(info['eta']))
    return info


def _notice(layout, text, icon='INFO', alert=False, detail=""):
    """A message inside its own box, optionally with a second info line."""
    box = layout.box()
    row = box.row()
    row.alert = alert
    row.label(text=text, icon=icon)
    if detail:
        box.label(text=detail, icon='INFO')


def draw_warning_blocks(layout, settings, scene, render):
    """Shared alert boxes for the settings panel and dialogs."""
    if not getattr(render, "save_output", True):
        _notice(layout, "'Save Output' is off in Output properties", 'ERROR',
                alert=True, detail="Playblast enables it temporarily.")
    if bpy.app.background:
        _notice(layout, "Background mode: viewport render unavailable",
                'ERROR', alert=True)
    if settings.keyframesOnly:
        _notice(layout, "Uses the SELECTED objects' keys")
    if settings.useCameraView and scene.camera is None:
        _notice(layout, "No scene camera set", 'ERROR', alert=True,
                detail="'Playblast From Camera' falls back to the current "
                       "viewport view.")


def draw_preset_row(layout, settings):
    """Preset menu + save, with the one-click built-in presets underneath."""
    row = layout.row(align=True)
    row.menu(PLAYBLAST_MT_presets.bl_idname,
             text=(settings.preset_active or "Presets"), icon='PRESET')
    row.operator(PlayblastPresetSave.bl_idname, text="", icon='ADD')

    row = layout.row(align=True)
    for name in BUILTIN_PRESETS:
        op = row.operator(PlayblastPresetLoad.bl_idname, text=name,
                          depress=(settings.preset_active == name))
        op.preset_name = name


def _section_box(layout, label, icon='NONE'):
    """
    One titled box per section, the same look as the summary box at the top
    of the panel.  Returns the box: draw the section's controls into it.
    Boxes bring their own padding, so no manual separators are needed.
    """
    box = layout.box()
    box.label(text=label, icon=icon)
    return box


def _toggle_with_field(layout, owner, toggle, field, field_text):
    """A checkbox and its value field on one row; the field greys out when off."""
    row = layout.row(align=True)
    row.prop(owner, toggle)
    sub = row.row(align=True)
    sub.active = bool(getattr(owner, toggle))
    sub.prop(owner, field, text=field_text)


def _section_resolution(layout, owner):
    layout.prop(owner, "resolutionMode", text="Mode")
    if owner.resolutionMode == 'LIST':
        layout.prop(owner, "resolutionTemplate", text="Preset", icon='PRESET')
        layout.prop(owner, "resolution_percentage")
    elif owner.resolutionMode == 'CUSTOM':
        col = layout.column(align=True)
        col.prop(owner, "resolutionX")
        col.prop(owner, "resolutionY")
        layout.prop(owner, "resolution_percentage")
    else:
        layout.label(text="Uses the current scene resolution", icon='INFO')


def _section_range(layout, owner):
    layout.prop(owner, "frameRangeMode", text="Mode")
    if owner.frameRangeMode == 'CUSTOM':
        col = layout.column(align=True)
        col.prop(owner, "frameRangeStart")
        col.prop(owner, "frameRangeEnd")
        if owner.frameRangeEnd < owner.frameRangeStart:
            row = layout.row()
            row.alert = True
            row.label(text="End must be >= Start", icon='ERROR')
    elif owner.frameRangeMode == 'PREVIEW':
        layout.label(text="Preview range enabled temporarily", icon='INFO')
    _toggle_with_field(layout, owner, "useFrameStep", "frameStep", "Step")
    layout.prop(owner, "keyframesOnly")


def _section_output(layout, owner):
    layout.prop(owner, "saveOutputToFile")
    if not owner.saveOutputToFile:
        layout.label(text="Renders a temporary PNG sequence", icon='INFO')
        layout.prop(owner, "autoPlay")
        return

    scene = bpy.context.scene
    layout.prop(owner, "outputFilePath", text="Folder")
    layout.prop(owner, "outputFileName", text="Name")
    # Live result of folder + name + tokens + version + format.
    layout.label(text=preview_filename(owner, scene), icon='FILE_TICK')
    layout.prop(owner, "useNameTokens")
    if owner.useNameTokens:
        hint = layout.column(align=True)
        hint.active = False          # greyed: reference text, not a control
        for line in TOKEN_HELP_LINES:
            hint.label(text=line)
    _toggle_with_field(layout, owner, "autoVersion", "versionPadding", "Digits")

    layout.prop(owner, "outputEncoding", text="Format")
    if owner.outputEncoding != 'IMAGE':
        layout.prop(owner, "outputQuality")
    else:
        layout.prop(owner, "imageEncoding")
    layout.prop(owner, "autoPlay")

    notice = output_directory_notice(owner)
    if notice:
        row = layout.row()
        row.alert = True
        row.label(text=notice, icon='ERROR')

    target_dir = os.path.dirname(resolve_render_filepath(owner, scene))
    if target_dir and not directory_is_writable(target_dir):
        row = layout.row()
        row.alert = True
        row.label(text="Folder is not writable!", icon='ERROR')


def _section_audio(layout, owner):
    layout.prop(owner, "audioCodec")
    if owner.audioCodec != 'NONE':
        layout.prop(owner, "audioBitrate")
        layout.prop(owner, "audioVolume")
        layout.prop(owner, "useAudioAdvanced")
        if owner.useAudioAdvanced:
            layout.prop(owner, "audioChannels")
            layout.prop(owner, "audioMixrate")


def _audio_applies(owner):
    """Audio only exists in a saved movie, never in sequences or temp output."""
    return owner.saveOutputToFile and owner.outputEncoding != 'IMAGE'


def _section_stamps(layout, owner):
    layout.prop(owner, "metadata_resolution")
    if owner.saveOutputToFile and owner.autoVersion:
        layout.prop(owner, "metadata_version")
    layout.prop(owner, "metadata_scene_name")
    layout.prop(owner, "metadata_camera_name")
    layout.prop(owner, "metadata_camera_lens")
    layout.prop(owner, "extraInfo", text="Note")
    layout.prop(owner, "stampAutoSize")
    if not owner.stampAutoSize:
        layout.prop(owner, "stampFontSize")
    layout.prop(owner, "useStampStyle")
    if owner.useStampStyle:
        layout.prop(owner, "stampUseLabels")
        layout.prop(owner, "stampFgColor", text="Text")
        layout.prop(owner, "stampBgColor", text="Background")


def _section_viewport(layout, owner):
    layout.prop(owner, "useCameraView")
    layout.prop(owner, "ColorMode", text="Shading")
    if owner.ColorMode == 'SOLID':
        layout.prop(owner, "SolidMode", text="Color")
    if owner.ColorMode != 'CURRENT':
        layout.prop(owner, "restore_shading")
    layout.prop(owner, "autoDisableOverlays")
    if owner.autoDisableOverlays:
        layout.prop(owner, "overlayMode", text="Mode")
        if owner.overlayMode == 'CUSTOM':
            for attr, _target, _props, label, _default in DISPLAY_TOGGLES:
                if not hasattr(owner, attr):
                    continue
                layout.prop(owner, attr, text=label)


# (label, icon, draw function, visible predicate or None) - single source of
# truth for the boxes of the settings panel, ordered what -> where -> look.
SETTINGS_SECTIONS = (
    ("Resolution",      'FULLSCREEN_ENTER', _section_resolution, None),
    ("Frame Range",     'TIME',             _section_range,      None),
    ("Output",          'FILE_FOLDER',      _section_output,     None),
    ("Viewport",        'VIEW3D',           _section_viewport,   None),
    ("Audio",           'SOUND',            _section_audio,      _audio_applies),
    ("Metadata Stamps", 'FONT_DATA',        _section_stamps,     None),
)


# History rows stat their file on EVERY panel redraw - invisible locally,
# laggy on network drives.  Results are cached briefly: the panel redraws on
# every mouse move, and a file appearing/vanishing mid-hover does not need
# to show up instantly.
_FILE_EXISTS_TTL = 2.0
_file_exists_cache = {}


def file_exists_cached(path, ttl=_FILE_EXISTS_TTL):
    """os.path.isfile with a short-lived cache, for constantly-redrawn UI."""
    if not path:
        return False
    now = time.time()
    hit = _file_exists_cache.get(path)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    try:
        exists = os.path.isfile(path)
    except OSError:
        exists = False
    _file_exists_cache[path] = (now, exists)
    return exists


def draw_history(layout, owner, settings):
    """Recent playblasts, newest first (two rows per entry: actions + time)."""
    try:
        count = len(settings.history)
    except Exception:
        count = 0

    if count == 0:
        layout.label(text="No playblasts yet", icon='INFO')
        return

    drawn = False
    for index in range(count):
        item = history_entry(settings, index)
        if item is None:
            continue
        if drawn:
            layout.separator()          # small gap BETWEEN entries only
        drawn = True
        # One aligned block per entry: its rows sit flush together.
        col = layout.column(align=True)
        exists = file_exists_cached(item.file)
        row = col.row(align=True)
        op = row.operator("my.playblast_play_history", text="",
                          icon='PLAY' if exists else 'ERROR')
        op.index = index
        label = item.label or os.path.basename(item.path or "?")
        if item.frames:
            label = "%s (%d f)" % (label, item.frames)
        row.label(text=label)
        if exists:
            op2 = row.operator("my.playblast_open_folder", text="", icon='FILE_FOLDER')
            op2.filepath = os.path.dirname(item.file)
        col.label(text=item.stamp, icon='TIME')

    # A toggle on the settings operator itself (see PlayblastSettings): a
    # separate operator would fight the redo panel's undo/re-execute cycle.
    layout.prop(owner, "clear_history", toggle=True, icon='TRASH')


def draw_settings_ui(layout, owner, settings, context):
    """
    The complete settings UI as ONE panel - nothing is collapsed; sections that cannot apply (e.g. audio for image sequences) are hidden,
    each in its own titled box (matching the summary box) instead of
    collapsible dropdowns.
    Drawn inside the Adjust Last Operation panel (viewport bottom-left).
    Rows use normal, non-aligned spacing so unrelated controls never fuse
    into one block; align=True is reserved for genuinely paired fields.
    Interactive controls come from `owner` (the settings operator) because
    the redo panel reverts scene-property edits; read-only read-outs come
    from the scene group.  Layout is split-free and single-column: the
    panel is narrow.
    """
    scene = getattr(context, "scene", None) or bpy.context.scene
    render = scene.render

    layout.use_property_split = False
    layout.use_property_decorate = False

    draw_preset_row(layout, settings)
    draw_summary(layout, settings, scene, icon='INFO')
    draw_warning_blocks(layout, settings, scene, render)

    for label, icon, draw_func, visible in SETTINGS_SECTIONS:
        if visible is None or visible(owner):
            draw_func(_section_box(layout, label, icon), owner)

    draw_history(_section_box(layout, "History", 'RECOVER_LAST'), owner, settings)

    layout.use_property_split = False


# =========================================================================
#  Playblast pipeline
# =========================================================================

NO_VIEWPORT_MESSAGE = "No 3D Viewport found - open one and try again."


class PlayblastJob:
    """
    End-to-end playblast pipeline:

        preflight()  -> side-effect-free validation (dialog AND execute)
        run()        -> prepare -> render -> verify -> record -> playback,
                        with a StateSnapshot restored in `finally`.
    """

    def __init__(self, operator, context):
        self.op = operator
        self.context = context
        self.scene = context.scene
        self.render = self.scene.render
        self.settings = self.scene.playblast_settings
        self.info = estimate_output(self.settings, self.scene)
        self.want_video = output_is_video(self.settings)
        self.target_path, self.version = resolve_output_path(self.settings,
                                                             scene=self.scene)
        self.area = None
        self.space = None
        # (width, height) burned into the stamp note: the final output size,
        # matching what the encoder actually writes.
        self.note_size = (self.info['width'], self.info['height'])

    # ------------------------------------------------------------ preflight
    @staticmethod
    def preflight(context):
        """
        Validation shared by invoke() (dialog) and execute() (EXEC_DEFAULT
        from Python).  Returns (ok, message).  Side effects are limited to
        creating the output folder and writing a probe file, and only when
        'Save to File' is enabled.
        """
        if bpy.app.background:
            return False, ("Playblast needs a window with a 3D Viewport "
                           "(Blender is running in background mode).")

        scene = context.scene
        settings = scene.playblast_settings

        _, area, space, region = find_view3d_context(context)
        if area is None or space is None or region is None:
            return False, NO_VIEWPORT_MESSAGE

        # NOTE: no hard error for useCameraView without a scene camera - the
        # option is on by default, so camera-less scenes fall back to the
        # current viewport view with a warning (see _prepare_viewport).

        if settings.frameRangeMode == 'CUSTOM' and \
                settings.frameRangeStart == settings.frameRangeEnd:
            return False, "Custom frame range needs at least two different frames."

        info = estimate_output(settings, scene)
        if info['frames'] <= 0:
            return False, "The selected frame range is empty (end before start)."

        if settings.saveOutputToFile:
            directory = os.path.dirname(resolve_render_filepath(settings, scene=scene))
            if directory and not os.path.isdir(directory):
                try:
                    os.makedirs(directory, exist_ok=True)
                except OSError:
                    return False, "Could not create the output folder: %s" % directory
            if directory and not directory_write_probe(directory):
                return False, "Output folder is not writable: %s" % directory

        return True, ""

    # ---------------------------------------------------------- preparation
    def _prepare_range(self):
        scene = self.scene
        settings = self.settings
        info = self.info
        if settings.frameRangeMode == 'PREVIEW':
            set_attr_safe(scene, 'use_preview_range', True)
        elif settings.frameRangeMode == 'CUSTOM':
            set_attr_safe(scene, 'use_preview_range', False)
            set_attr_safe(scene, 'frame_start', info['start'])
            set_attr_safe(scene, 'frame_end', info['end'])
        # Only override the step when asked to; otherwise the scene's own
        # frame_step is used (and restored untouched).
        if settings.useFrameStep:
            set_attr_safe(scene, 'frame_step', info['step'])
        return True

    def _prepare_save_output(self):
        render = self.render
        if getattr(render, 'save_output', True):
            return True
        if not set_attr_safe(render, 'save_output', True):
            report(self.op, 'ERROR',
                   "Render output saving is disabled in the Output panel "
                   "and could not be enabled automatically.")
            return False
        report(self.op, 'INFO',
               "'Save Output' was disabled - enabled it for this playblast.")
        return True

    def _prepare_resolution(self):
        # estimate_output() already resolved mode/template/percentage into the
        # final size (self.note_size), so write that directly for videos and
        # images alike: the encoder gets baked pixels at 100% and the stamp
        # note matches what is actually written.
        w, h = self.note_size
        if self.want_video:
            # Movie encoders need even dimensions, otherwise FFmpeg fails.
            w += w % 2
            h += h % 2
        render = self.render
        render.resolution_x = w
        render.resolution_y = h
        render.resolution_percentage = 100
        self.note_size = (w, h)
        return True

    def _prepare_output(self):
        render = self.render
        render.use_overwrite = True
        render.use_file_extension = True
        ok, message = apply_output_format(render, self.settings, self.want_video)
        if not ok:
            report(self.op, 'ERROR', message)
            return False
        render.filepath = (movie_filepath(render, self.target_path)
                           if self.want_video else self.target_path)
        return True

    def _prepare_stamps(self):
        width, height = self.note_size
        note_text = build_stamp_note(self.settings, width, height,
                                     self.version if self.settings.autoVersion else 0)
        apply_stamp_settings(self.render, self.settings, note_text, height)
        return True

    def _prepare_viewport(self):
        settings = self.settings
        space = self.space
        apply_display(space, settings)
        if settings.ColorMode != 'CURRENT':
            set_shading_on_space(space, settings.ColorMode, settings.SolidMode)
        rv3d = get_attr_safe(space, "region_3d", None)
        if settings.useCameraView:
            if self.scene.camera is None:
                report(self.op, 'WARNING',
                       "'Playblast From Camera' is on but the scene has no "
                       "camera - playblasting from the current viewport view.")
            elif rv3d is None or \
                    assign_enum(rv3d, "view_perspective", ['CAMERA']) is None:
                report(self.op, 'WARNING',
                       "Could not switch the viewport to camera view.")
        try:
            self.area.tag_redraw()
        except Exception:
            pass
        return True

    # --------------------------------------------------------------- render
    def _render(self):
        raw = execute_in_view3d(self.context, bpy.ops.render.opengl,
                                animation=True, view_context=True,
                                render_keyed_only=bool(self.settings.keyframesOnly))
        if raw is None:
            report(self.op, 'ERROR', "Could not find a 3D Viewport to render from.")
            return False

        op_result = normalize_operator_result(raw)
        if 'CANCELLED' in op_result or 'FINISHED' not in op_result:
            report(self.op, 'ERROR',
                   "Viewport render was cancelled. Check that no other render "
                   "job is running and that a 3D Viewport is visible.")
            return False
        return True

    # -------------------------------------------------- verify and record
    def _verify_and_record(self, started):
        settings = self.settings
        info = self.info
        elapsed = time.time() - started

        # Only files from THIS run count: stale output from an earlier run
        # would otherwise make verification pass even when this render wrote
        # nothing ('playblast_temp_' especially collides with old runs).
        # The cutoff is anchored to `started` (not now - elapsed) with 2 s
        # slack: file timestamps on network volumes can drift seconds from
        # the system clock, and rejecting a valid file is worse than the
        # tiny chance of letting a seconds-old stale one through.
        cutoff = started - 2.0
        found = find_output_files(self.target_path, self.want_video)
        written = [p for p in found if os.path.getmtime(p) >= cutoff]
        if not written:
            report(self.op, 'ERROR',
                   "The render finished but no output file was found at "
                   "'{}'. Check the output path and 'Save Output'."
                   .format(self.target_path))
            return False
        rendered_file = written[-1]

        if settings.saveOutputToFile and not self.want_video:
            frame_exts = tuple(IMAGE_FORMAT_EXTENSIONS.values())
            leftover = [p for p in found if p not in written
                        and p.lower().endswith(frame_exts)]
            if leftover:
                report(self.op, 'WARNING',
                       "{} older frame(s) from a previous playblast share this "
                       "name and will play with the new sequence. Enable Auto "
                       "Version or delete them.".format(len(leftover)))

        # Record only now that we know it worked.
        settings.last_playblast_path = self.target_path
        settings.last_playblast_format = (settings.outputEncoding
                                          if settings.saveOutputToFile else 'IMAGE')
        settings.last_playblast_use_ffmpeg = self.want_video
        settings.last_playblast_encoding = ("" if self.want_video
                                            else settings.imageEncoding)
        settings.last_playblast_file = rendered_file
        settings.last_render_seconds = elapsed
        settings.last_render_frames = max(1, info['frames'])

        push_history(settings, self.target_path, rendered_file, self.want_video,
                     settings.last_playblast_format,
                     settings.last_playblast_encoding,
                     info['frames'], elapsed)

        suffix = (" (version %d)" % self.version) if self.version else ""
        report(self.op, 'INFO',
               "Playblast: {} frames in {}{} -> {}".format(
                   info['frames'], format_duration(elapsed), suffix,
                   rendered_file))
        return True

    # ------------------------------------------------------------- playback
    def _playback(self):
        # Runs while the render's frame range is still applied: image
        # sequences are located from the scene's first frame.
        settings = self.settings
        return play_output(self.context, self.op, self.target_path,
                           self.want_video, settings.outputEncoding,
                           settings.imageEncoding)

    # ------------------------------------------------------------------ run
    def run(self):
        """Executes the pipeline. Returns the operator result status set."""
        context = self.context
        settings = self.settings

        _win, area, space, region = find_view3d_context(context)
        if area is None or space is None or region is None:
            report(self.op, 'ERROR', NO_VIEWPORT_MESSAGE)
            return {'CANCELLED'}
        self.area, self.space = area, space

        notice = output_directory_notice(settings)
        if notice:
            report(self.op, 'WARNING', notice)

        snapshot = StateSnapshot(context, self.render, space)
        started = time.time()
        status = {'CANCELLED'}
        # Each step returns True to continue; the first failure stops the job.
        steps = (self._prepare_range, self._prepare_save_output,
                 self._prepare_resolution, self._prepare_output,
                 self._prepare_stamps, self._prepare_viewport, self._render)
        try:
            if all(step() for step in steps):
                if self._verify_and_record(started):
                    status = {'FINISHED'}
                    if settings.autoPlay:
                        self._playback()
        except Exception as exc:
            LOG.exception("Playblast failed")
            report(self.op, 'ERROR', "Playblast failed: {}".format(exc))
        finally:
            snapshot.restore(context, settings)
        return status


def play_output(context, operator, base_path, is_video, container, image_encoding):
    """
    Shared playback implementation: points render.filepath/format at an
    existing playblast, runs `render.play_rendered_anim`, then restores.
    """
    render = context.scene.render
    settings = context.scene.playblast_settings

    written = find_output_files(base_path, is_video)
    if not written:
        report(operator, 'ERROR',
               "No playblast output found at '{}'. Run PLAYBLAST first."
               .format(base_path))
        return False

    if not hasattr(bpy.ops.render, "play_rendered_anim"):
        report(operator, 'ERROR',
               "This Blender build has no 'render.play_rendered_anim' operator.")
        return False

    image_settings = render.image_settings
    snap = {
        'filepath': render.filepath,
        'use_file_extension': render.use_file_extension,
        'file_format': image_settings.file_format,
        'media_type': get_media_type(image_settings),
        'ffmpeg_format': (get_attr_safe(render.ffmpeg, "format")
                          if hasattr(render, "ffmpeg") else None),
    }

    try:
        # play_rendered_anim reads render.filepath and the format at call time.
        render.use_file_extension = True

        # Use the format the playblast was RENDERED with (passed in from the
        # history/last-run bookkeeping), not what the UI would render next.
        chosen_container = None
        if is_video:
            chosen_container = (container if container not in ('', 'IMAGE')
                                else settings.outputEncoding)
        ok, message = apply_output_format(
            render, settings, is_video,
            container=chosen_container,
            image_format=(image_encoding or 'PNG') if not is_video else None)
        if not ok:
            report(operator, 'ERROR', message)
            return False

        render.filepath = movie_filepath(render, base_path) if is_video else base_path
        res = normalize_operator_result(bpy.ops.render.play_rendered_anim())
        if 'CANCELLED' in res:
            report(operator, 'ERROR',
                   "Could not start the animation player. Set it in "
                   "Edit > Preferences > File Paths > Animation Player.")
            return False

        report(operator, 'INFO', "Playing {}".format(written[-1]))
        return True

    except Exception as exc:
        LOG.exception("Playback failed")
        report(operator, 'ERROR', "Playback failed: {}".format(exc))
        return False

    finally:
        ffmpeg = getattr(render, "ffmpeg", None)
        if ffmpeg is not None and snap['ffmpeg_format']:
            assign_enum(ffmpeg, "format", [snap['ffmpeg_format']])
        restore_output_format(render, snap['file_format'], snap['media_type'])
        set_attr_safe(render, 'filepath', snap['filepath'])
        set_attr_safe(render, 'use_file_extension', snap['use_file_extension'])


# =========================================================================
#  Operators
# =========================================================================

class PlayblastSettings(Operator):
    """Open the Playblast settings in the Adjust Last Operation panel"""
    bl_idname = "my.playblast_settings"
    bl_label = "Playblast Settings"
    bl_description = (
        "Configure resolution, output format, stamps and viewport "
        "display in the Adjust Last Operation panel (viewport bottom-left)"
    )
    bl_options = {'REGISTER', 'UNDO'}

    # A real RNA property, NOT a plain class attribute: Blender
    # recreates the operator on every redo-panel edit and only
    # carries RNA properties across.
    ui_session: BoolProperty(name="UI Session", default=False,
                             options={'HIDDEN'})

    # One-shot button drawn in the History box.  Every redo-panel edit undoes
    # to the state before this operator and re-runs it, which would bring a
    # history cleared by a SEPARATE operator straight back; clearing inside
    # execute() runs after that undo, so it sticks.  Operator-only: it is not
    # in the scene group, so it is never mirrored or saved in presets.
    clear_history: BoolProperty(
        name="Clear History", default=False, options={'SKIP_SAVE'},
        description="Remove all entries from the recent playblasts list")

    def invoke(self, context, event):
        self.ui_session = True
        sync_pg_to_operator(self, context.scene.playblast_settings)
        return self.execute(context)

    def draw(self, context):
        draw_settings_ui(
            self.layout,
            self,
            context.scene.playblast_settings,
            context
        )

    def execute(self, context):
        settings = context.scene.playblast_settings

        if not self.ui_session:
            sync_pg_to_operator(self, settings)
            return {'FINISHED'}

        sync_operator_to_pg(self, settings)
        if self.clear_history:
            self.clear_history = False       # one-shot: untick for the redraw
            try:
                settings.history.clear()
            except Exception as exc:
                report(self, 'ERROR', "Could not clear history: %s" % exc)
                return {'CANCELLED'}
            _file_exists_cache.clear()       # its paths belonged to the rows
            report(self, 'INFO', "History cleared.")
        return {'FINISHED'}


# Mirror every interactive setting onto the operator, reusing the exact same
# property definitions as the scene property group (single source of truth).
for _name in MIRRORED_NAMES:
    PlayblastSettings.__annotations__[_name] = \
        PlayblastSettingsGroup.__annotations__[_name]


class Playblast(Operator):
    """Render an animation preview from the active 3D Viewport"""
    bl_idname = "my.playblast"
    bl_label = "Playblast"
    bl_description = "Render the viewport animation with the playblast settings and play it back"
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        ok, message = PlayblastJob.preflight(context)
        if not ok:
            report(self, 'ERROR', message)
            return {'CANCELLED'}
        return open_confirm_dialog(context.window_manager, self, width=480,
                                   title="Confirm Playblast",
                                   confirm_text="PLAYBLAST")

    def draw(self, context):
        layout = self.layout
        settings = context.scene.playblast_settings
        draw_summary(layout, settings, context.scene, icon='RENDER_ANIMATION')

        box = layout.box()
        col = box.column(align=True)
        row = col.row()
        row.alert = True
        row.label(text="Blender will be unresponsive while rendering", icon='ERROR')
        col.label(text="Viewport renders called from Python are blocking and")
        col.label(text="cannot be cancelled with Esc - this is Blender's own")
        col.label(text="behaviour, not a limitation of this add-on.")

        notice = output_directory_notice(settings)
        if notice:
            row = layout.row()
            row.alert = True
            row.label(text=notice, icon='ERROR')

        target, _version = resolve_output_path(settings, scene=context.scene,
                                               create_dirs=False)
        layout.label(text="Output: %s" % target, icon='FILE_TICK')

    def execute(self, context):
        # Works both from the confirm dialog (INVOKE_DEFAULT) and when called
        # directly as bpy.ops.my.playblast() (EXEC_DEFAULT), so validation
        # always runs.
        ok, message = PlayblastJob.preflight(context)
        if not ok:
            report(self, 'ERROR', message)
            return {'CANCELLED'}
        return PlayblastJob(self, context).run()


class ViewPlayblast(Operator):
    """Play back the last rendered playblast"""
    bl_idname = "my.playblast_play"
    bl_label = "View Playblast"
    bl_description = "Play back the last rendered playblast in the animation player"
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.scene.playblast_settings
        if not settings.last_playblast_path:
            report(self, 'ERROR', "Nothing to play - run a Playblast first.")
            return {'CANCELLED'}
        ok = play_output(context, self,
                         settings.last_playblast_path,
                         bool(settings.last_playblast_use_ffmpeg),
                         settings.last_playblast_format,
                         settings.last_playblast_encoding)
        return {'FINISHED'} if ok else {'CANCELLED'}


class ViewPlayblastHistory(Operator):
    """Play back a playblast from the history list"""
    bl_idname = "my.playblast_play_history"
    bl_label = "Play History Entry"
    bl_description = "Play this entry in the animation player"
    # No REGISTER: a registered operator becomes the 'last operation' and
    # would replace the settings panel (this one changes no settings).

    index: IntProperty(name="Index", default=0)

    def execute(self, context):
        settings = context.scene.playblast_settings
        item = history_entry(settings, self.index)
        if item is None:
            report(self, 'ERROR', "That history entry no longer exists.")
            return {'CANCELLED'}
        if not item.path:
            report(self, 'ERROR', "That entry has no recorded path.")
            return {'CANCELLED'}
        ok = play_output(context, self, item.path, bool(item.is_video),
                         item.fmt, item.encoding)
        return {'FINISHED'} if ok else {'CANCELLED'}


class PlayblastOpenFolder(Operator):
    """Open the folder containing this playblast"""
    bl_idname = "my.playblast_open_folder"
    bl_label = "Open Output Folder"
    bl_description = "Open the output folder in the system file browser"
    # No REGISTER: a registered operator becomes the 'last operation' and
    # would replace the settings panel (this one changes no settings).

    filepath: StringProperty(name="Folder", default="", subtype='DIR_PATH')

    def execute(self, context):
        target = self.filepath
        if not target:
            settings = context.scene.playblast_settings
            base = settings.last_playblast_file or settings.last_playblast_path
            target = os.path.dirname(base) if base else ""
        if not target:
            target = os.path.dirname(
                resolve_render_filepath(context.scene.playblast_settings,
                                        scene=context.scene))
        if not target or not os.path.isdir(target):
            report(self, 'ERROR', "Folder does not exist: %s" % (target or "(none)"))
            return {'CANCELLED'}
        try:
            bpy.ops.wm.path_open(filepath=target)
        except Exception as exc:
            report(self, 'ERROR', "Could not open the folder: %s" % exc)
            return {'CANCELLED'}
        return {'FINISHED'}


class PlayblastPresetSave(Operator):
    """Save the current playblast settings as a named preset"""
    bl_idname = "my.playblast_preset_save"
    bl_label = "Save Playblast Preset"
    bl_description = "Save the current settings as a reusable preset"
    bl_options = {'REGISTER'}

    preset_name: StringProperty(name="Preset Name", default="my_playblast")

    def invoke(self, context, event):
        if not self.preset_name or self.preset_name == "my_playblast":
            self.preset_name = (context.scene.playblast_settings.preset_active
                                or "playblast_preset")
        return open_confirm_dialog(context.window_manager, self, width=340,
                                   title="Save Preset", confirm_text="Save")

    def execute(self, context):
        ok, message = save_preset(self.preset_name, context.scene.playblast_settings)
        if not ok:
            report(self, 'ERROR', "Could not save preset: %s" % message)
            return {'CANCELLED'}
        set_active_preset(context.scene.playblast_settings,
                          os.path.splitext(os.path.basename(message))[0])
        report(self, 'INFO', "Preset saved to %s" % message)
        reopen_settings()
        return {'FINISHED'}


class PlayblastPresetLoad(Operator):
    """Load the selected playblast preset"""
    bl_idname = "my.playblast_preset_load"
    bl_label = "Load Playblast Preset"
    bl_description = "Apply this preset to the playblast settings"
    bl_options = {'REGISTER', 'UNDO'}

    preset_name: StringProperty(name="Preset Name", default="")

    def execute(self, context):
        settings = context.scene.playblast_settings
        name = self.preset_name or settings.preset_active
        if not name:
            report(self, 'ERROR', "Choose a preset from the menu first.")
            return {'CANCELLED'}
        ok, message = load_preset(name, settings)
        if ok:
            set_active_preset(settings, name)
        report(self, 'INFO' if ok else 'ERROR', message)
        if ok:
            reopen_settings()
        return {'FINISHED'} if ok else {'CANCELLED'}


class PlayblastPresetDelete(Operator):
    """Delete the selected playblast preset"""
    bl_idname = "my.playblast_preset_delete"
    bl_label = "Delete Playblast Preset"
    bl_description = "Delete the selected preset file"
    bl_options = {'REGISTER'}

    preset_name: StringProperty(name="Preset Name", default="")

    def invoke(self, context, event):
        if not self.preset_name:
            self.preset_name = context.scene.playblast_settings.preset_active
        if not self.preset_name:
            report(self, 'ERROR', "Choose a preset from the menu first.")
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        settings = context.scene.playblast_settings
        name = self.preset_name or settings.preset_active
        ok, message = delete_preset(name)
        report(self, 'INFO' if ok else 'ERROR', message)
        if ok:
            set_active_preset(settings, '')
            reopen_settings()
        return {'FINISHED'} if ok else {'CANCELLED'}


# =========================================================================
#  Preset menu
# =========================================================================

class PLAYBLAST_MT_presets(Menu):
    """List of saved playblast presets"""
    bl_label = "Playblast Presets"
    bl_idname = "PLAYBLAST_MT_presets"
    bl_description = "Load, save or delete playblast presets"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.playblast_settings
        names = list_presets()
        active = settings.preset_active

        def add_entry(name):
            icon = 'RADIOBUT_ON' if name == active else 'RADIOBUT_OFF'
            op = layout.operator(PlayblastPresetLoad.bl_idname, text=name, icon=icon)
            op.preset_name = name

        for name in BUILTIN_PRESETS:
            add_entry(name)
        if names:
            layout.separator()
            for name in names:
                add_entry(name)

        layout.separator()
        layout.operator(PlayblastPresetSave.bl_idname, text="Save Preset...", icon='ADD')
        if active and active in names:
            op = layout.operator(PlayblastPresetDelete.bl_idname,
                                 text="Delete '%s'" % active, icon='REMOVE')
            op.preset_name = active


# =========================================================================
#  Sidebar panel (three buttons, nothing else)
# =========================================================================

class PlayblastMainPanel(Panel):
    bl_label = "Playblast"
    bl_category = "Playblast"
    bl_space_type = "VIEW_3D"
    bl_region_type = 'UI'

    def draw(self, context):
        layout = self.layout
        
        col = layout.column(align=False)
        col.scale_y = 1.8
        col.operator("my.playblast", text="", icon='RENDER_ANIMATION')
        col.operator("my.playblast_play", text="", icon='PLAY')
        col.operator("my.playblast_settings", text="", icon='PREFERENCES')


# =========================================================================
#  Registration
# =========================================================================

def menu_func(self, context):
    self.layout.separator()
    self.layout.operator(Playblast.bl_idname, icon="RENDER_ANIMATION")
    self.layout.operator(ViewPlayblast.bl_idname, icon="PLAY")
    self.layout.operator(PlayblastSettings.bl_idname, icon="PREFERENCES")


classes = (
    PlayblastHistoryItem,
    PlayblastSettingsGroup,
    Playblast,
    ViewPlayblast,
    ViewPlayblastHistory,
    PlayblastOpenFolder,
    PlayblastPresetSave,
    PlayblastPresetLoad,
    PlayblastPresetDelete,
    PlayblastSettings,
    PLAYBLAST_MT_presets,
    PlayblastMainPanel,
)

MENU_NAMES = ("VIEW3D_MT_view",)


def _menus():
    """The menu classes that exist in this Blender version."""
    found = (getattr(bpy.types, name, None) for name in MENU_NAMES)
    return [menu for menu in found if menu is not None]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.playblast_settings = PointerProperty(type=PlayblastSettingsGroup)

    for menu in _menus():
        try:
            menu.append(menu_func)
        except Exception:
            pass


def unregister():
    for menu in _menus():
        try:
            menu.remove(menu_func)
        except Exception:
            pass

    if hasattr(bpy.types.Scene, "playblast_settings"):
        try:
            del bpy.types.Scene.playblast_settings
        except Exception:
            pass

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


if __name__ == "__main__":
    register()
