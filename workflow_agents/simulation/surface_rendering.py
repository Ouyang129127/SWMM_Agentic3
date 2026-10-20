"""Shared display-only terrain and land layers for CA2D result maps."""
import re
from pathlib import Path

import numpy as np
from matplotlib.colors import LightSource, to_rgb
from matplotlib.patches import Patch


BASEMAP_PALETTE = {
    "land": "#cad7be",
    "road": "#d5dcdf",
    "road_edge": "#99a3a6",
    "building": "#79736d",
    "building_edge": "#514c47",
    "contour": "#75836f",
}
MAP_RC = {
    "font.sans-serif": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
    "axes.unicode_minus": False,
    "font.size": 10,
    "figure.facecolor": "white",
    "savefig.facecolor": "white",
}


def load_display_layers(model_dir, config, elevation, flow_mask, building_mask):
    """Read optional display layers without changing solver arrays or files."""
    model_dir = Path(model_dir)
    road_path = model_dir / "road_mask.npy"
    valid_path = model_dir / "valid_mask.npy"
    terrain_path = model_dir / "terrain.npy"
    road = np.load(road_path).astype(bool) if road_path.exists() else np.zeros_like(flow_mask, dtype=bool)
    valid = np.load(valid_path).astype(bool) if valid_path.exists() else (flow_mask | building_mask)
    terrain = None
    if terrain_path.exists():
        terrain = np.load(terrain_path)
    else:
        # Existing UrbanDrainage builds describe their exact synthetic offsets
        # in config.json. New builds save unmodified terrain.npy directly.
        raised = re.search(r"raised by ([0-9.]+) m\b", config.get("building_method", ""))
        lowered = re.search(r"lowered by ([0-9.]+) m\b", config.get("road_method", ""))
        if raised:
            terrain = np.array(elevation, dtype=float, copy=True)
            terrain[np.asarray(building_mask, dtype=bool)] -= float(raised.group(1))
            if lowered:
                terrain[road] += float(lowered.group(1))
    return {"road_mask": road, "valid_mask": valid, "terrain": terrain,
            "cell_size": float(config["cell_size"]), "road_known": road_path.exists()}


def _fill_display_gaps(values, known):
    """Interpolate roofs/outside-domain pixels only for display derivatives."""
    result = np.where(known, values, np.nan)
    for row in result:
        good = np.isfinite(row)
        if good.any():
            row[:] = np.interp(np.arange(row.size), np.flatnonzero(good), row[good])
    for col in result.T:
        good = np.isfinite(col)
        if good.any():
            col[:] = np.interp(np.arange(col.size), np.flatnonzero(good), col[good])
    return np.nan_to_num(result)


def prepare_basemap(elevation, flow_mask, building_mask, *, road_mask=None,
                    valid_mask=None, terrain=None, cell_size=1.0, road_known=None):
    elevation = np.asarray(elevation)
    flow = np.asarray(flow_mask, dtype=bool)
    building = np.asarray(building_mask, dtype=bool)
    valid = np.asarray(valid_mask, dtype=bool) if valid_mask is not None else (flow | building)
    road = np.asarray(road_mask, dtype=bool) if road_mask is not None else np.zeros_like(flow)
    if elevation.ndim != 2 or any(a.shape != elevation.shape for a in (flow, building, valid, road)):
        raise ValueError("CA2D display layers must share one two-dimensional grid")
    if not np.isfinite(cell_size) or cell_size <= 0:
        raise ValueError("CA2D display cell size must be finite and positive")
    road = road & flow & valid & ~building
    building = building & valid
    land = valid & ~road & ~building
    if terrain is None:
        # With no documented roof height, use neighboring ground for shading.
        ground = _fill_display_gaps(elevation, valid & ~building & np.isfinite(elevation))
    else:
        ground = np.array(terrain, dtype=float, copy=True)
        if ground.shape != elevation.shape or not np.isfinite(ground[valid]).all():
            raise ValueError("CA2D display terrain must match the grid and be finite")
    if valid.any():
        lo, hi = np.percentile(ground[valid], [2, 98])
    else:
        lo = hi = 0.0
    height = np.clip((ground-lo)/max(hi-lo, 1e-9), 0, 1)
    gy = np.gradient(ground, cell_size, axis=0) if ground.shape[0] > 1 else np.zeros_like(ground)
    gx = np.gradient(ground, cell_size, axis=1) if ground.shape[1] > 1 else np.zeros_like(ground)
    normals = np.dstack([-gx, gy, np.ones_like(gx)]) / np.sqrt(1+gx*gx+gy*gy)[..., None]
    shade = LightSource(azdeg=315, altdeg=50).shade_normals(normals)
    brightness = .89 + .16*height + .10*(shade-.5)
    layer = np.zeros((*elevation.shape, 4))
    layer[..., :3] = np.clip(np.array(to_rgb(BASEMAP_PALETTE["land"])) * brightness[..., None], 0, 1)
    layer[..., 3] = valid
    # Keep 1 m contours for this urban case; avoid dense contour noise on
    # larger elevation ranges in other model projects.
    interval = max(1.0, float(np.ceil((hi-lo)/24)))
    levels = np.arange(np.ceil(lo/interval)*interval, hi, interval)
    return {"rgba": layer, "ground": ground, "land": land, "road": road,
            "building": building, "valid": valid, "levels": levels,
            "road_known": road_mask is not None if road_known is None else road_known}


def _solid_layer(mask, color):
    layer = np.zeros((*mask.shape, 4))
    layer[mask, :3] = to_rgb(color)
    layer[mask, 3] = 1
    return layer


def draw_basemap(ax, layers):
    p = BASEMAP_PALETTE
    ax.set_facecolor("white")
    ax.imshow(layers["rgba"], interpolation="nearest")
    contour_grid = min(layers["valid"].shape) > 1
    if contour_grid and len(layers["levels"]) and layers["land"].any():
        ax.contour(np.ma.masked_where(~layers["land"], layers["ground"]),
                   levels=layers["levels"], colors=p["contour"], linewidths=.35, alpha=.27)
    for key, color, edge, width, order in (
        ("road", p["road"], p["road_edge"], .42, 3),
        ("building", p["building"], p["building_edge"], .6, 5),
    ):
        mask = layers[key]
        ax.imshow(_solid_layer(mask, color), interpolation="nearest", zorder=order)
        if contour_grid and mask.any() and not mask.all():
            ax.contour(mask.astype(float), levels=[.5], colors=edge, linewidths=width, zorder=order+1)
    valid = layers["valid"]
    if contour_grid and valid.any() and not valid.all():
        ax.contour(valid.astype(float), levels=[.5], colors="#a7ada5", linewidths=.6, zorder=7)
    ax.set_xlim(-.5, valid.shape[1]-.5)
    ax.set_ylim(valid.shape[0]-.5, -.5)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)


def basemap_legend(road_known=True):
    p = BASEMAP_PALETTE
    items = [Patch(facecolor=p["land"], label="其余土地 / 地形")]
    if road_known:
        items.append(Patch(facecolor=p["road"], edgecolor=p["road_edge"], label="道路"))
    items.append(Patch(facecolor=p["building"], edgecolor=p["building_edge"], label="建筑物"))
    return items
