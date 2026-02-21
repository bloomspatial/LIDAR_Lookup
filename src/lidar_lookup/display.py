"""
Display a LAZ/LAS point cloud in a 3D PyVista window.

Requires optional dependencies: pip install lidar-lookup[display]
"""

from __future__ import annotations

from pathlib import Path

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    import pyvista as pv

MAX_DISPLAY_POINTS = 10_000_000

# Fly speed (world units per key press)
FLY_STEP = 50.0
# Rotation angle per key press (radians)
ROTATE_STEP = 0.08


def _setup_wasd_fly(plotter: "pv.Plotter", step: float = FLY_STEP) -> None:
    """Register WASD (+ Q/E) for fly; Shift+WASD for rotate (uppercase = rotate)."""
    import numpy as np  # type: ignore[import-untyped]

    def _normalize(v: "np.ndarray") -> "np.ndarray":
        n = np.linalg.norm(v)
        return v / n if n > 1e-10 else v

    def _rotate_vector(v: "np.ndarray", axis: "np.ndarray", angle: float) -> "np.ndarray":
        """Rotate vector v around unit axis by angle (radians). Rodrigues."""
        axis = _normalize(axis)
        return (
            v * np.cos(angle)
            + np.cross(axis, v) * np.sin(angle)
            + axis * np.dot(axis, v) * (1.0 - np.cos(angle))
        )

    def move(direction: "np.ndarray") -> None:
        pos, focal, up = plotter.camera_position
        pos = np.asarray(pos, dtype=float)
        focal = np.asarray(focal, dtype=float)
        up = np.asarray(up, dtype=float)
        pos += direction
        focal += direction
        plotter.camera_position = [pos.tolist(), focal.tolist(), up.tolist()]
        plotter.update()

    def rotate_view(pitch_delta: float, yaw_delta: float) -> None:
        """Change view direction: pitch = around right, yaw = around up."""
        pos, focal, up = plotter.camera_position
        pos = np.asarray(pos, dtype=float)
        focal = np.asarray(focal, dtype=float)
        up = np.asarray(up, dtype=float)
        fwd = _normalize(focal - pos)
        right = _normalize(np.cross(fwd, up))
        if abs(pitch_delta) > 1e-10:
            fwd = _rotate_vector(fwd, right, pitch_delta)
            up = _rotate_vector(up, right, pitch_delta)
        if abs(yaw_delta) > 1e-10:
            fwd = _rotate_vector(fwd, up, yaw_delta)
        dist = np.linalg.norm(np.asarray(focal) - pos)
        new_focal = pos + dist * _normalize(fwd)
        plotter.camera_position = [pos.tolist(), new_focal.tolist(), up.tolist()]
        plotter.update()

    def forward() -> None:
        pos, focal, up = plotter.camera_position
        pos = np.asarray(pos, dtype=float)
        focal = np.asarray(focal, dtype=float)
        fwd = _normalize(focal - pos)
        move(step * fwd)

    def backward() -> None:
        pos, focal, up = plotter.camera_position
        pos = np.asarray(pos, dtype=float)
        focal = np.asarray(focal, dtype=float)
        fwd = _normalize(focal - pos)
        move(-step * fwd)

    def strafe_right() -> None:
        pos, focal, up = plotter.camera_position
        pos = np.asarray(pos, dtype=float)
        focal = np.asarray(focal, dtype=float)
        up = np.asarray(up, dtype=float)
        fwd = _normalize(focal - pos)
        right = _normalize(np.cross(fwd, up))
        move(step * right)

    def strafe_left() -> None:
        pos, focal, up = plotter.camera_position
        pos = np.asarray(pos, dtype=float)
        focal = np.asarray(focal, dtype=float)
        up = np.asarray(up, dtype=float)
        fwd = _normalize(focal - pos)
        right = _normalize(np.cross(fwd, up))
        move(-step * right)

    def fly_up() -> None:
        _, _, up = plotter.camera_position
        up = np.asarray(up, dtype=float)
        move(step * _normalize(up))

    def fly_down() -> None:
        _, _, up = plotter.camera_position
        up = np.asarray(up, dtype=float)
        move(-step * _normalize(up))

    # Lowercase = move (fly)
    plotter.add_key_event("w", forward)
    plotter.add_key_event("s", backward)
    plotter.add_key_event("a", strafe_left)
    plotter.add_key_event("d", strafe_right)
    # Uppercase = Shift+key = rotate (pitch/yaw)
    plotter.add_key_event("W", lambda: rotate_view(ROTATE_STEP, 0.0))   # look up
    plotter.add_key_event("S", lambda: rotate_view(-ROTATE_STEP, 0.0))  # look down
    plotter.add_key_event("A", lambda: rotate_view(0.0, ROTATE_STEP))    # turn left
    plotter.add_key_event("D", lambda: rotate_view(0.0, -ROTATE_STEP))  # turn right
    plotter.add_key_event("e", fly_up)
    plotter.add_key_event("E", fly_up)
    plotter.add_key_event("q", fly_down)
    plotter.add_key_event("Q", fly_down)


def _load_one(
    path: Path,
    decimate: int | None,
) -> tuple["pv.PolyData", bool]:
    """Load one LAZ/LAS into a PyVista PolyData. Returns (cloud, has_rgb)."""
    import laspy  # type: ignore[import-untyped]
    import numpy as np  # type: ignore[import-untyped]
    import pyvista as pv  # type: ignore[import-untyped]

    if not path.exists():
        raise FileNotFoundError(f"No such file: {path}")

    print(f"Reading {path} ...")
    with laspy.open(path) as fh:
        las = fh.read()

    x = np.array(las.x)
    y = np.array(las.y)
    z = np.array(las.z)
    n = len(x)
    print(f"Loaded {n:,} points")
    if n == 0:
        raise ValueError(f"Point cloud is empty: {path}")

    if decimate is None:
        step = max(1, n // MAX_DISPLAY_POINTS)
    else:
        step = max(1, decimate)
    if step > 1:
        print(f"Decimating by {step}x -> {n // step:,} displayed")

    points = np.column_stack((x, y, z))[::step]

    has_color = False
    rgb = None
    if hasattr(las, "red") and hasattr(las, "green") and hasattr(las, "blue"):
        try:
            r = np.array(las.red)[::step]
            g = np.array(las.green)[::step]
            b = np.array(las.blue)[::step]
            if r.size == points.shape[0]:
                rgb = np.column_stack((r, g, b))
                if rgb.max() > 255:
                    rgb = (rgb / 256).astype(np.uint8)
                elif rgb.max() > 1:
                    rgb = rgb.astype(np.uint8)
                else:
                    rgb = (rgb * 255).astype(np.uint8)
                has_color = True
        except Exception:
            pass

    cloud = pv.PolyData(points)
    if has_color and rgb is not None:
        cloud["RGB"] = rgb
    else:
        cloud["Elevation"] = points[:, 2]
    return cloud, has_color


def display_laz(
    path: str | Path | list[str] | list[Path],
    decimate: int | None = None,
) -> None:
    """
    Open one or more LAZ/LAS files and show an interactive 3D point cloud viewer.

    With a single file: uses RGB if the point cloud has color dimensions;
    otherwise points are colored by elevation (Z). With multiple files, a
    uniform palette is used: elevation uses a global min/max so the same Z
    maps to the same color; RGB is normalized to a global range. Large files are
    automatically decimated to ~10M points for responsiveness (set decimate=1 to force
    plotting every point).

    Raises:
        ImportError: If laspy or pyvista are not installed (install with [display]).
        FileNotFoundError: If a path does not exist.
    """
    import numpy as np  # type: ignore[import-untyped]
    import pyvista as pv  # type: ignore[import-untyped]

    paths: list[Path]
    if isinstance(path, (str, Path)):
        paths = [Path(path)]
    else:
        paths = [Path(p) for p in path]
    if not paths:
        raise ValueError("At least one path is required")

    clouds: list[tuple[pv.PolyData, bool, str]] = []
    for p in paths:
        cloud, has_rgb = _load_one(p, decimate)
        color_mode = "RGB" if has_rgb else "elevation"
        print(f"Building point cloud {p.name} ({cloud.n_points:,} points, color={color_mode}) ...")
        clouds.append((cloud, has_rgb, p.name))

    try:
        pv.set_jupyter_backend(None)
    except Exception:
        pass
    print("Opening viewer (close the window to exit) ...")
    plotter = pv.Plotter(window_size=[2560, 1440])
    plotter.set_background("black")
    _setup_wasd_fly(plotter)

    if len(clouds) == 1:
        cloud, has_rgb, name = clouds[0]
        if has_rgb:
            plotter.add_mesh(cloud, scalars="RGB", rgb=True, point_size=1.5, render_points_as_spheres=False)
        else:
            plotter.add_mesh(cloud, scalars="Elevation", cmap="terrain", point_size=1.5, render_points_as_spheres=False)
        plotter.add_title(f"{name}  |  WASD fly, Shift+WASD rotate, Q/E up-down", font_size=10)
    else:
        # Uniform palette: use global elevation range so same Z = same color across all files
        all_have_rgb = all(has_rgb for _, has_rgb, _ in clouds)
        if all_have_rgb:
            # Normalize RGB to global range so the same intensity = same color across files
            all_rgb = np.vstack([c[0]["RGB"] for c in clouds])
            r_min, r_max = all_rgb[:, 0].min(), all_rgb[:, 0].max()
            g_min, g_max = all_rgb[:, 1].min(), all_rgb[:, 1].max()
            b_min, b_max = all_rgb[:, 2].min(), all_rgb[:, 2].max()
            for i, (cloud, _has_rgb, name) in enumerate(clouds):
                rgb = cloud["RGB"].astype(np.float64)
                if r_max > r_min:
                    rgb[:, 0] = (rgb[:, 0] - r_min) / (r_max - r_min) * 255
                if g_max > g_min:
                    rgb[:, 1] = (rgb[:, 1] - g_min) / (g_max - g_min) * 255
                if b_max > b_min:
                    rgb[:, 2] = (rgb[:, 2] - b_min) / (b_max - b_min) * 255
                cloud["RGB"] = np.clip(rgb, 0, 255).astype(np.uint8)
                plotter.add_mesh(
                    cloud,
                    scalars="RGB",
                    rgb=True,
                    point_size=1.5,
                    render_points_as_spheres=False,
                )
        else:
            # Elevation with global clim so palette is uniform across files
            z_all = np.hstack([c[0]["Elevation"] for c in clouds])
            z_min, z_max = float(np.min(z_all)), float(np.max(z_all))
            if z_max <= z_min:
                z_max = z_min + 1.0
            clim = (z_min, z_max)
            for i, (cloud, _has_rgb, name) in enumerate(clouds):
                plotter.add_mesh(
                    cloud,
                    scalars="Elevation",
                    cmap="terrain",
                    clim=clim,
                    show_scalar_bar=(i == 0),
                    scalar_bar_args={"title": "Elevation", "n_labels": 6} if i == 0 else {},
                    point_size=1.5,
                    render_points_as_spheres=False,
                )
        title = " / ".join(c[2] for c in clouds)
        if len(title) > 80:
            title = f"{len(clouds)} files: " + ", ".join(c[2] for c in clouds[:3])
            if len(clouds) > 3:
                title += f" ... +{len(clouds) - 3} more"
        plotter.add_title(f"{title}  |  WASD fly, Shift+WASD rotate, Q/E up-down", font_size=10)

    plotter.show(full_screen=False)
    print("Viewer closed.")
